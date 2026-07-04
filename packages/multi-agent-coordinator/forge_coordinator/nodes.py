"""Supervisor graph nodes — all deterministic, NO LLM (F27 §3.3).

Each node is a ``(SupervisionState, CoordinatorDeps) -> SupervisionState`` function.
The LLM work happens only inside the subagents (``deps.agent_factory``); the nodes
themselves select patterns, gate spawns, dispatch/merge/validate, and finalize via
explicit Python predicates over typed state.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from forge_contracts import (
    CODE_PRODUCING_ROLES,
    AgentObjective,
    AgentRunResult,
    AgentRuntime,
    MergeResult,
    RepoChangeSet,
    RetrievedChunk,
    RunStatus,
    Step,
    StepKind,
    SubAgentAssignment,
    SubAgentResult,
    SubAgentRole,
)
from forge_coordinator.aggregate import aggregate_confidence, validate_acceptance
from forge_coordinator.artifacts import build_subagent_result, normalize_artifact_to_chunks
from forge_coordinator.deps import CoordinatorDeps
from forge_coordinator.objectives import build_subagent_objective
from forge_coordinator.persistence import SubAgentRunCreate, result_status_to_run
from forge_coordinator.policy_gate import evaluate_gate
from forge_coordinator.redaction import redact_obj
from forge_coordinator.state import SupervisionState

__all__ = [
    "dispatch",
    "finalize",
    "merge_node",
    "policy_gate_node",
    "select_pattern",
    "validate_node",
]


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #
def _record_step(
    state: SupervisionState,
    deps: CoordinatorDeps,
    *,
    node: str,
    thought: str,
    agent_run_ids: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    meta: dict[str, Any] = {"node": node, "agent_run_ids": agent_run_ids or []}
    if metadata:
        meta.update(redact_obj(metadata))
    step = Step(kind=StepKind.DECISION, thought=thought, metadata=meta)
    state.steps.append(step)
    if deps.step_sink is not None:
        deps.step_sink(step)
    if deps.audit_sink is not None:
        deps.audit_sink(
            {
                "event": f"supervisor.{node}",
                "parent_agent_run_id": str(state.parent_agent_run_id),
                "thought": thought,
                "agent_run_ids": agent_run_ids or [],
            }
        )


def _handoff_context(
    state: SupervisionState, assignment: SubAgentAssignment
) -> list[RetrievedChunk]:
    chunks: list[RetrievedChunk] = []
    for dep in assignment.depends_on:
        res = state.results.get(dep)
        if res is None:
            continue
        chunks.extend(
            normalize_artifact_to_chunks(res.artifact, assignment_id=dep, role=res.role)
        )
    return chunks


def _run_agent(deps: CoordinatorDeps, objective: AgentObjective) -> AgentRunResult:
    agent: AgentRuntime = deps.agent_factory()
    return agent.run(objective)


def _prepare(
    state: SupervisionState, deps: CoordinatorDeps, assignment: SubAgentAssignment
) -> tuple[AgentObjective, str | None]:
    """Build the scoped objective + worktree for one assignment and persist a row."""
    context_refs = _handoff_context(state, assignment)
    assignment = assignment.model_copy(update={"context_refs": context_refs})
    state.assignments[assignment.id] = assignment

    branch_name: str | None = None
    worktree_path: str | None = None
    if (
        assignment.role in CODE_PRODUCING_ROLES
        and state.repo is not None
        and state.ws_manager is not None
    ):
        handle = state.ws_manager.create_subagent_branch(
            integration_branch=state.integration_branch, assignment_id=assignment.id
        )
        state.handles[assignment.id] = handle
        branch_name = handle.branch_name
        worktree_path = str(handle.worktree_path)

    objective = build_subagent_objective(
        parent=state.objective,
        assignment=assignment,
        repo=state.repo,
        integration_branch=state.integration_branch,
        worktree_path=worktree_path,
        branch_name=branch_name,
    )

    persisted_objective = {
        "objective": objective.objective,
        "role": assignment.role.value,
        "allowed_actions": list(objective.allowed_actions),
        "model": objective.model,
    }
    existing = state.row_ids.get(assignment.id)
    if existing is not None:
        deps.sub_agent_sink.update(existing, status=RunStatus.RUNNING)
    else:
        row_id = deps.sub_agent_sink.create(
            SubAgentRunCreate(
                parent_agent_run_id=state.parent_agent_run_id,
                workspace_id=state.workspace_id,
                assignment_id=assignment.id,
                role=assignment.role,
                pattern=state.plan.pattern.value if state.plan else "",
                ordinal=int(assignment.ordinal),
                objective=persisted_objective,
                depends_on=list(assignment.depends_on),
                optional=assignment.optional,
                status=RunStatus.RUNNING,
            )
        )
        state.row_ids[assignment.id] = row_id
    return objective, branch_name


def _fold(
    state: SupervisionState,
    deps: CoordinatorDeps,
    assignment: SubAgentAssignment,
    result: SubAgentResult,
) -> None:
    state.results[assignment.id] = result
    status = result.status
    if status in {"failed", "blocked"} and assignment.optional:
        status = "skipped"
    state.statuses[assignment.id] = status
    row_id = state.row_ids.get(assignment.id)
    if row_id is not None:
        error = None
        if result.status in {"failed", "blocked"}:
            error = {"type": "subagent_error", "message": result.artifact.summary}
        deps.sub_agent_sink.update(
            row_id,
            status=result_status_to_run(result.status),
            artifact=result.artifact.model_dump(mode="json"),
            confidence=result.confidence,
            branch_name=result.artifact.branch_name,
            token_usage=result.token_usage.model_dump(),
            agent_run_id=result.agent_run_id,
            output={"summary": result.artifact.summary},
            error=error,
            completed_at=datetime.now(UTC),
        )


# --------------------------------------------------------------------------- #
# nodes                                                                        #
# --------------------------------------------------------------------------- #
def select_pattern(state: SupervisionState, deps: CoordinatorDeps) -> SupervisionState:
    plan = deps.pattern_selector.select(
        objective=state.objective,
        policy=None,
        subagent_rules=state.subagent_rules,
        task_subagent_policy=state.task_subagent_policy,
        directives=state.directives,
    )
    state.plan = plan
    state.assignments = {a.id: a for a in plan.assignments}
    state.statuses = {a.id: "pending" for a in plan.assignments}
    state.review_loop_budget = deps.settings.review_loop_budget
    _record_step(
        state,
        deps,
        node="select_pattern",
        thought=f"selected pattern {plan.pattern.value}",
        metadata={
            "pattern": plan.pattern.value,
            "assignments": [a.id for a in plan.assignments],
        },
    )
    return state


def policy_gate_node(state: SupervisionState, deps: CoordinatorDeps) -> SupervisionState:
    assert state.plan is not None
    gate = evaluate_gate(
        plan=state.plan,
        subagent_rules=state.subagent_rules,
        task_subagent_policy=state.task_subagent_policy,
        settings=deps.settings,
    )
    state.max_parallel = max(gate.max_parallel, 1) if gate.ok else 0

    for aid in gate.skipped:
        state.statuses[aid] = "skipped"
        assignment = state.assignments[aid]
        from forge_coordinator.artifacts import ARTIFACT_KIND_BY_ROLE

        row_id = deps.sub_agent_sink.create(
            SubAgentRunCreate(
                parent_agent_run_id=state.parent_agent_run_id,
                workspace_id=state.workspace_id,
                assignment_id=aid,
                role=assignment.role,
                pattern=state.plan.pattern.value,
                ordinal=int(assignment.ordinal),
                depends_on=list(assignment.depends_on),
                optional=assignment.optional,
                status=RunStatus.CANCELLED,
            )
        )
        state.row_ids[aid] = row_id
        from forge_contracts import SubAgentArtifact

        state.results[aid] = SubAgentResult(
            assignment_id=aid,
            role=assignment.role,
            status="skipped",
            confidence=0.0,
            artifact=SubAgentArtifact(
                kind=ARTIFACT_KIND_BY_ROLE[assignment.role],  # type: ignore[arg-type]
                summary="skipped: role not permitted by subagent_rules",
            ),
        )

    if not gate.ok:
        state.policy_conflict = gate.reason
        state.needs_human = True
        state.needs_human_reason = gate.reason
        for aid, status in list(state.statuses.items()):
            if status == "pending":
                state.statuses[aid] = "blocked"

    _record_step(
        state,
        deps,
        node="policy_gate",
        thought=f"policy gate {'ok' if gate.ok else 'blocked: ' + str(gate.reason)}",
        metadata={
            "ok": gate.ok,
            "reason": gate.reason,
            "max_parallel": state.max_parallel,
            "skipped": sorted(gate.skipped),
        },
    )
    return state


def dispatch(state: SupervisionState, deps: CoordinatorDeps) -> SupervisionState:
    ready = state.ready_assignments()
    if not ready:
        return state

    prepared: list[tuple[SubAgentAssignment, AgentObjective, str | None]] = []
    for assignment in ready:
        state.statuses[assignment.id] = "running"
        objective, branch = _prepare(state, deps, assignment)
        prepared.append((assignment, objective, branch))

    # Execute child agent runs, bounded by max_parallel. Worktrees are already
    # created (sequentially, above); only the agent runs go parallel.
    children: dict[str, AgentRunResult] = {}
    if len(prepared) > 1 and state.max_parallel > 1:
        with ThreadPoolExecutor(max_workers=state.max_parallel) as pool:
            futures = {
                a.id: pool.submit(_run_agent, deps, obj) for (a, obj, _b) in prepared
            }
            for aid, fut in futures.items():
                children[aid] = fut.result()
    else:
        for assignment, objective, _branch in prepared:
            children[assignment.id] = _run_agent(deps, objective)

    for assignment, _objective, branch in prepared:
        child = children[assignment.id]
        result = build_subagent_result(
            assignment=state.assignments[assignment.id], child=child, branch_name=branch
        )
        _fold(state, deps, state.assignments[assignment.id], result)

    _record_step(
        state,
        deps,
        node="dispatch",
        thought="dispatched " + ", ".join(a.id for a, _o, _b in prepared),
        agent_run_ids=[
            str(state.results[a.id].agent_run_id)
            for a, _o, _b in prepared
            if state.results.get(a.id) and state.results[a.id].agent_run_id
        ],
        metadata={
            "dispatched": [a.id for a, _o, _b in prepared],
            "statuses": {a.id: state.statuses[a.id] for a, _o, _b in prepared},
        },
    )

    _detect_terminal(state)
    _handle_reviewer_rejects(state, deps)
    return state


def _detect_terminal(state: SupervisionState) -> None:
    for aid in list(state.statuses):
        status = state.statuses[aid]
        assignment = state.assignments[aid]
        if status == "awaiting_input":
            state.needs_human = True
            state.needs_human_reason = state.needs_human_reason or f"subagent_awaiting_input:{aid}"
        elif status in {"failed", "blocked"} and not assignment.optional:
            state.needs_human = True
            state.needs_human_reason = state.needs_human_reason or f"subagent_failed:{aid}"


def _handle_reviewer_rejects(state: SupervisionState, deps: CoordinatorDeps) -> None:
    next_ordinal = max((int(a.ordinal) for a in state.assignments.values()), default=0) + 1
    for aid in list(state.results):
        result = state.results[aid]
        assignment = state.assignments[aid]
        if (
            assignment.role is not SubAgentRole.REVIEWER
            or aid in state.processed_rejects
            or result.status != "succeeded"
            or result.artifact.review_verdict != "changes_requested"
        ):
            continue
        state.processed_rejects.add(aid)
        impl_ids = [
            d
            for d in assignment.depends_on
            if state.assignments[d].role is SubAgentRole.IMPLEMENTER
        ]
        if state.review_loops >= state.review_loop_budget or not impl_ids:
            state.needs_human = True
            state.needs_human_reason = state.needs_human_reason or "review_rejected"
            continue

        state.review_loops += 1
        impl = state.assignments[impl_ids[0]]
        state.superseded.update(impl_ids)
        retry_impl = SubAgentAssignment(
            id=f"{impl.id}#r{state.review_loops}",
            role=SubAgentRole.IMPLEMENTER,
            objective=impl.objective,
            acceptance_criteria=list(impl.acceptance_criteria),
            allowed_actions=list(impl.allowed_actions),
            depends_on=[aid],  # receive the reviewer's findings as scoped context
            ordinal=next_ordinal,
            optional=impl.optional,
        )
        retry_reviewer = SubAgentAssignment(
            id=f"{assignment.id}#r{state.review_loops}",
            role=SubAgentRole.REVIEWER,
            objective=assignment.objective,
            acceptance_criteria=list(assignment.acceptance_criteria),
            allowed_actions=list(assignment.allowed_actions),
            depends_on=[retry_impl.id],
            ordinal=next_ordinal + 1,
            optional=assignment.optional,
        )
        for retry in (retry_impl, retry_reviewer):
            state.assignments[retry.id] = retry
            state.statuses[retry.id] = "pending"
        _record_step(
            state,
            deps,
            node="dispatch",
            thought=f"reviewer {aid} requested changes; re-dispatching {retry_impl.id}",
            metadata={"findings": result.artifact.findings, "loop": state.review_loops},
        )


def merge_node(state: SupervisionState, deps: CoordinatorDeps) -> SupervisionState:
    if state.repo is None or state.base_sha is None:
        state.merge_result = MergeResult(
            integration_branch=state.integration_branch,
            head_sha=None,
            diff_stat={"files": 0, "insertions": 0, "deletions": 0},
        )
        _record_step(state, deps, node="merge", thought="no repo; read-only merge")
        return state

    results_for_merge = [
        r
        for aid, r in state.results.items()
        if aid not in state.superseded and state.statuses.get(aid) == "succeeded"
    ]
    assert state.plan is not None
    merge_result = deps.merger.merge(
        repo=state.repo,
        integration_branch=state.integration_branch,
        results=results_for_merge,
        base_sha=state.base_sha,
        strategy=state.plan.merge_strategy,
    )
    state.merge_result = merge_result
    for aid in merge_result.merged_assignments:
        row_id = state.row_ids.get(aid)
        if row_id is not None:
            deps.sub_agent_sink.update(row_id, merged=True)
    if merge_result.conflicts:
        state.needs_human = True
        state.needs_human_reason = state.needs_human_reason or "merge_conflict"

    _record_step(
        state,
        deps,
        node="merge",
        thought=(
            f"merged {len(merge_result.merged_assignments)} branch(es), "
            f"{len(merge_result.conflicts)} conflict(s)"
        ),
        metadata={
            "merged": merge_result.merged_assignments,
            "conflicts": [c.model_dump() for c in merge_result.conflicts],
        },
    )
    return state


def validate_node(state: SupervisionState, deps: CoordinatorDeps) -> SupervisionState:
    reviewer_results = [
        r
        for aid, r in state.results.items()
        if r.role is SubAgentRole.REVIEWER and aid not in state.superseded
    ]
    reviewer_rejected = any(
        r.artifact.review_verdict == "changes_requested" for r in reviewer_results
    )
    reviewer_ok = not reviewer_rejected
    checks = validate_acceptance(
        criteria=list(state.objective.acceptance_criteria),
        merge=state.merge_result,
        reviewer_ok=reviewer_ok,
    )
    required_conf = [
        r.confidence
        for aid, r in state.results.items()
        if not state.assignments[aid].optional
        and aid not in state.superseded
        and state.statuses.get(aid) == "succeeded"
    ]
    agg = aggregate_confidence(
        required_confidences=required_conf,
        reviewer_rejected=reviewer_rejected,
        threshold=state.threshold,
    )
    state.acceptance_checks = checks
    state.aggregate_confidence = agg
    _record_step(
        state,
        deps,
        node="validate",
        thought=f"aggregate confidence {agg}; reviewer_rejected={reviewer_rejected}",
        metadata={
            "aggregate_confidence": agg,
            "unsatisfied": [c.id for c in checks if not c.satisfied],
        },
    )
    return state


def finalize(state: SupervisionState, deps: CoordinatorDeps) -> SupervisionState:
    checks = state.acceptance_checks
    satisfied = [c.id for c in checks if c.satisfied]
    unsatisfied = [c.id for c in checks if not c.satisfied]

    if state.aggregate_confidence is not None and state.aggregate_confidence < state.threshold:
        state.needs_human = True
        state.needs_human_reason = state.needs_human_reason or "low_confidence"
    if unsatisfied:
        state.needs_human = True
        state.needs_human_reason = state.needs_human_reason or "acceptance_unsatisfied"
    if state.policy_conflict:
        state.needs_human = True
        state.needs_human_reason = state.needs_human_reason or state.policy_conflict

    status = RunStatus.ESCALATED if state.needs_human else RunStatus.SUCCEEDED

    input_tokens = sum(r.token_usage.input_tokens for r in state.results.values())
    output_tokens = sum(r.token_usage.output_tokens for r in state.results.values())
    total = input_tokens + output_tokens

    repo_change_sets: list[RepoChangeSet] = []
    merge_result = state.merge_result
    if merge_result and merge_result.head_sha and merge_result.changed_files:
        repo_change_sets.append(
            RepoChangeSet(
                repo=state.repo or "",
                branch_name=merge_result.integration_branch,
                base_commit_sha=state.base_sha,
                head_commit_sha=merge_result.head_sha,
                changed_files=list(merge_result.changed_files),
                diff_stat=dict(merge_result.diff_stat),
                has_changes=True,
            )
        )

    artifacts: dict[str, Any] = {
        "needs_human_reason": state.needs_human_reason,
        "pattern": state.plan.pattern.value if state.plan else None,
        "is_supervisor": True,
        "policy_conflict": state.policy_conflict,
        "aggregate_confidence": state.aggregate_confidence,
        "token_usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total": total,
        },
        "merge": merge_result.model_dump(mode="json") if merge_result else None,
        "acceptance": [{"id": c.id, "satisfied": c.satisfied} for c in checks],
        "subagents": [
            {
                "assignment_id": r.assignment_id,
                "role": r.role.value,
                "status": r.status,
                "confidence": r.confidence,
                "branch_name": r.artifact.branch_name,
                "agent_run_id": str(r.agent_run_id) if r.agent_run_id else None,
            }
            for r in state.results.values()
        ],
    }

    result = AgentRunResult(
        run_id=state.parent_agent_run_id,
        task_id=state.objective.task_id,
        status=status,
        steps=list(state.steps),
        output=state.needs_human_reason if state.needs_human else "supervised run complete",
        summary=f"pattern={state.plan.pattern.value if state.plan else '?'}",
        confidence=state.aggregate_confidence,
        needs_human=state.needs_human,
        acceptance_criteria_satisfied=satisfied,
        artifacts=redact_obj(artifacts),
        repo_change_sets=repo_change_sets,
        error=state.error,
    )
    state.result = result
    _record_step(
        state,
        deps,
        node="finalize",
        thought=f"finalized status={status.value} needs_human={state.needs_human}",
        metadata={"needs_human_reason": state.needs_human_reason},
    )
    if state.ws_manager is not None:
        state.ws_manager.cleanup(keep_branches=True)
    return state
