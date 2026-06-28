"""The deterministic Supervisor (F27 §4).

``Supervisor`` implements the foundation's synchronous ``AgentRuntime`` protocol
(``run(objective) -> AgentRunResult``) so F07/F08 are agnostic to execution mode.
``resume`` continues a checkpointed supervision after a human-in-the-loop gate
(merge conflict, low confidence, child interrupt) without re-running completed
subagents.

Foundation conformance: the foundation ``AgentRuntime`` protocol has no async and
no ``resume`` member, and ``AgentRunResult`` has no flat ``branch_name``/
``diff_stat`` fields — those live on ``repo_change_sets``. The Supervisor matches
the real shapes; ``resume`` is an additional (non-protocol) method, and the
``needs_human_reason`` ("awaiting_input" semantics) is carried on
``artifacts["needs_human_reason"]`` with ``status=ESCALATED`` + ``needs_human``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forge_contracts import AgentObjective, AgentRunResult, SubagentRules
from forge_coordinator.deps import CoordinatorDeps
from forge_coordinator.gitutil import GitError
from forge_coordinator.graph import build_resume_graph, build_supervisor_graph
from forge_coordinator.state import SupervisionState
from forge_skill.directives import SkillDirectives, to_directives

__all__ = ["HumanResumeInput", "Supervisor"]


@dataclass
class HumanResumeInput:
    """A human's decision resuming a paused supervision (F27 §2 journey F/G)."""

    decision: str = "approve"  # approve|resume|reject
    reason: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


_RESUME_DECISIONS = {"approve", "resume", "retry", "continue"}


class Supervisor:
    """The deterministic, policy-driven multi-agent Supervisor."""

    def __init__(self, deps: CoordinatorDeps) -> None:
        self._deps = deps
        self._graph = build_supervisor_graph(deps)
        self._resume_graph = build_resume_graph(deps)
        self._suspended: dict[uuid.UUID, SupervisionState] = {}

    # ------------------------------------------------------------------ #
    # AgentRuntime protocol                                              #
    # ------------------------------------------------------------------ #
    def run(self, objective: AgentObjective) -> AgentRunResult:
        state = self._init_state(objective)
        final = self._graph.invoke(state)
        assert final.result is not None
        if final.needs_human:
            self._suspended[final.parent_agent_run_id] = final
        return final.result

    def resume(
        self, agent_run_id: uuid.UUID, human_input: HumanResumeInput | None = None
    ) -> AgentRunResult:
        human_input = human_input or HumanResumeInput()
        state = self._suspended.pop(agent_run_id, None)
        if state is None:
            raise KeyError(f"no suspended supervision for {agent_run_id}")

        state.needs_human = False
        state.needs_human_reason = None
        decision = (human_input.decision or "approve").strip().lower()
        if decision in _RESUME_DECISIONS:
            for aid, status in list(state.statuses.items()):
                if status == "awaiting_input":
                    state.statuses[aid] = "pending"
        else:
            # A reject keeps the run paused; finalize as awaiting_input again.
            for aid, status in list(state.statuses.items()):
                if status == "awaiting_input":
                    state.statuses[aid] = "blocked"
            state.needs_human = True
            state.needs_human_reason = human_input.reason or "rejected_by_human"

        final = self._resume_graph.invoke(state)
        assert final.result is not None
        if final.needs_human:
            self._suspended[final.parent_agent_run_id] = final
        return final.result

    # ------------------------------------------------------------------ #
    # Setup                                                              #
    # ------------------------------------------------------------------ #
    def _init_state(self, objective: AgentObjective) -> SupervisionState:
        ctx = objective.context
        parent_id = _as_uuid(ctx.get("parent_agent_run_id")) or uuid.uuid4()
        workspace_id = _as_uuid(ctx.get("workspace_id")) or uuid.uuid4()

        subagent_rules = _resolve_subagent_rules(objective)
        task_policy = objective.subagent_policy
        directives = _resolve_directives(objective)

        target = objective.primary_repo_target
        base_branch = target.base_branch if target else "main"
        repo = target.repo if target else None
        integration_branch = str(
            ctx.get("integration_branch")
            or f"forge/{objective.key or (objective.task_id or 'task')}"
        )

        ws_manager = None
        base_sha = None
        usable_repo: str | None = None
        if repo is not None and Path(repo).is_dir():
            try:
                ws_manager = self._deps.workspace_factory(repo)
                base_sha = ws_manager.ensure_integration_branch(
                    base_branch=base_branch, integration_branch=integration_branch
                )
                usable_repo = repo
            except GitError:
                ws_manager = None
                base_sha = None
                usable_repo = None

        return SupervisionState(
            objective=objective,
            parent_agent_run_id=parent_id,
            workspace_id=workspace_id,
            subagent_rules=subagent_rules,
            task_subagent_policy=task_policy,
            directives=directives,
            threshold=self._deps.settings.confidence_threshold,
            repo=usable_repo,
            base_branch=base_branch,
            integration_branch=integration_branch,
            base_sha=base_sha,
            ws_manager=ws_manager,
            review_loop_budget=self._deps.settings.review_loop_budget,
        )


def _as_uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError:
            return None
    return None


def _resolve_subagent_rules(objective: AgentObjective) -> SubagentRules:
    raw = objective.context.get("subagent_rules")
    if isinstance(raw, SubagentRules):
        return raw
    if isinstance(raw, dict):
        return SubagentRules.model_validate(raw)
    sp = objective.subagent_policy
    return SubagentRules(
        allow_subagents=sp.allowed,
        allowed_roles=list(sp.allowed_roles),
        max_parallel=sp.max_parallel,
    )


def _resolve_directives(objective: AgentObjective) -> SkillDirectives:
    if objective.skill_profile is not None:
        return to_directives(objective.skill_profile)
    review_required = bool(objective.context.get("review_required", False))
    return SkillDirectives(name="default", review_required=review_required)
