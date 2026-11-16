"""Real services for the Temporal workflow's activities (issue #170).

Each function here returns the callable ``WorkflowActivities`` invokes for one
activity. An activity with no service here keeps refusing (ADR-0007).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from temporalio.exceptions import ApplicationError

from scf_api.observability.redaction import redact_text
from scf_api.services.agent_dispatch import STRIPPED_CONTEXT, repo_connected
from scf_contracts import AgentObjective, AgentRunResult, ExecutionMode
from scf_contracts import RunStatus as ResultStatus
from scf_contracts.run_events import RunEventDraft, RunEventType
from scf_db.models import (
    AgentRun,
    AgentRunEvent,
    KnowledgeSource,
    SkillProfile,
    Task,
    WorkflowRun,
)
from scf_db.models.enums import RunStatus
from scf_worker.agent_runner import _commits_ahead, run_agent_objective
from scf_worker.approval_activity import make_create_approval_fn
from scf_worker.dispatch import (
    RepositoryUnavailable,
    objective_for_task,
    resolve_run_repository,
    split_repo_name,
    workspace_mirrors,
)
from scf_worker.finalizer import GitHubAppCredentials, Pusher, git_push, push_and_open_pr
from scf_worker.review_rules import default_branch_policy
from scf_worker.run_events import RunEventSink
from scf_worker.run_workspace import NO_DECLARED_CHECKS, CheckRun, run_declared_checks
from scf_workflow.red_team_gate import ensure_red_team_verdict
from scf_workflow.temporal.activities import (
    ApprovalFn,
    GuardInputsFn,
    NotifyFn,
    OpenPrFn,
    RedTeamFn,
    RunAgentFn,
    RunChecksFn,
)
from scf_workflow.temporal.payloads import (
    AgentRunResultDTO,
    ApprovalInput,
    ChecksResult,
    GuardInputs,
    GuardInputsRequest,
    NotifyInput,
    OpenPrInput,
    OpenPrResult,
    RedTeamInput,
    RedTeamResult,
    RunAgentInput,
    RunChecksInput,
)

__all__ = [
    "agent_run_service",
    "approval_service",
    "guard_inputs_service",
    "notify_service",
    "open_pr_service",
    "red_team_service",
    "run_checks_service",
]

#: The run-context key naming the optional preconditions a workspace has not configured.
NOT_IN_USE_KEY = "preconditions_not_in_use"


def _owned_run(session: Session, run_id: uuid.UUID, workspace_id: uuid.UUID) -> WorkflowRun:
    """The workflow run, or a refusal when it is not in the caller's workspace."""
    run = session.get(WorkflowRun, run_id)
    if run is None or run.workspace_id != workspace_id:
        raise ApplicationError(
            f"workflow run {run_id} does not exist in workspace {workspace_id}",
            type="WorkflowRunNotFound",
            non_retryable=True,
        )
    return run


def red_team_service(factory: sessionmaker[Session]) -> RedTeamFn:
    """Record the run's red-team verdict once. No adversary is wired (#174 adds
    one), so the row is the explicit ``parked`` verdict the V1 engine records."""

    def scan(inp: RedTeamInput) -> RedTeamResult:
        with factory() as session:
            _owned_run(session, inp.workflow_run_id, inp.workspace_id)
            row, _ = ensure_red_team_verdict(
                session,
                inp.workspace_id,
                workflow_run_id=inp.workflow_run_id,
                task_id=inp.task_id,
                phase=inp.phase,
                coder_model=inp.coder_model,
            )
            session.commit()
            return RedTeamResult(
                verdict=row.verdict,
                kind=row.kind,
                evidence=dict(row.evidence),
                adversary_model=row.adversary_model,
                coder_model=row.coder_model,
            )

    return scan


def approval_service(factory: sessionmaker[Session]) -> ApprovalFn:
    """Open the gate's approval request, refusing a run outside the caller's workspace.

    The request itself is opened by ``approval_activity.make_create_approval_fn``.
    """
    create = make_create_approval_fn(factory)

    async def create_for_owned_run(inp: ApprovalInput) -> uuid.UUID:
        with factory() as session:
            _owned_run(session, inp.workflow_run_id, inp.workspace_id)
        return await create(inp)

    return create_for_owned_run


def _policy_loads(workspace_id: uuid.UUID, repo: str) -> bool:
    """The default branch's policy file parses, or there is none and the default applies.

    The mirror is fetched first when GitHub App credentials exist, so the file is the
    default branch's current one; with no credentials and no mirror nothing is readable.
    """
    objective = AgentObjective(
        objective="guard inputs",
        context={"repo_full_name": repo, "workspace_id": str(workspace_id)},
    )
    try:
        mirror = Path(str(resolve_run_repository(objective).context["repo_path"]))
    except RepositoryUnavailable:
        return False
    try:
        default_branch_policy(mirror)
    except (ValueError, yaml.YAMLError):  # a git failure raises on, so the activity retries
        return False
    return True


def guard_inputs_service(factory: sessionmaker[Session]) -> GuardInputsFn:
    """The execute preconditions, read from stored data (#250, owner decisions 2026-11-15).

    A repository target on the task is always required, connected to the workspace
    (the check the API makes before a run), and so is a policy that loads; an
    unconnected target is not fetched, and the workflow waits at the guard. A skill
    profile and a knowledge sync are required only once the workspace has configured
    one; the run records which of the two were not in use. Every phase keeps the plan
    gate.
    """

    def load(req: GuardInputsRequest) -> GuardInputs:
        with factory() as session:
            run = _owned_run(session, req.workflow_run_id, req.workspace_id)
            if req.phase != "execute":
                return GuardInputs(plan_required=True)
            task = session.get(Task, run.task_id)
            profiles = set(
                session.scalars(
                    select(SkillProfile.name).where(SkillProfile.workspace_id == req.workspace_id)
                )
            )
            synced_at = list(
                session.scalars(
                    select(KnowledgeSource.last_synced_at).where(
                        KnowledgeSource.workspace_id == req.workspace_id
                    )
                )
            )
            targets = task.repo_targets if task is not None else []
            skill_profile = task.skill_profile if task is not None else None
            target = AgentObjective(
                objective="guard inputs", repo_targets=targets
            ).primary_repo_target
            connected = target is not None and repo_connected(
                session, req.workspace_id, target.repo
            )
        preconditions = {
            "repo_target_set": connected,
            "policy_loaded": connected
            and target is not None
            and _policy_loads(req.workspace_id, target.repo),
            "skill_profile_set": not profiles or skill_profile in profiles,
            "knowledge_synced": all(at is not None for at in synced_at),
        }
        not_in_use = [
            name
            for name, configured in (
                ("skill_profile_set", profiles),
                ("knowledge_synced", synced_at),
            )
            if not configured
        ]
        with factory() as session:
            run = _owned_run(session, req.workflow_run_id, req.workspace_id)
            if run.context.get(NOT_IN_USE_KEY) != not_in_use:
                run.context = {**run.context, NOT_IN_USE_KEY: not_in_use}
                session.commit()
        return GuardInputs(plan_required=True, preconditions=preconditions)

    return load


def _workflow_result(agent_run_id: uuid.UUID, result: AgentRunResult) -> AgentRunResultDTO:
    """The agent result the workflow routes on: ``succeeded``, or ``failed`` with why."""
    succeeded = result.status == ResultStatus.SUCCEEDED
    return AgentRunResultDTO(
        agent_run_id=agent_run_id,
        status="succeeded" if succeeded else "failed",
        # No confidence claimed is not low confidence: the runtime's own rule.
        confidence=1.0 if result.confidence is None else result.confidence,
        needs_human_reason=None if succeeded else (result.error or result.status.value),
        branch_name=result.artifacts.get("branch"),
        head_commit_sha=result.artifacts.get("head_commit"),
    )


def _kept_objective(context: dict[str, Any]) -> AgentObjective | None:
    """The objective a run was started with (issue dispatch keeps one), if any.

    Server-owned and tracker keys are stripped from its context, as at every other
    entry point: only SCF sets those, here from the run itself.
    """
    raw = (context.get("trigger") or {}).get("objective")
    if not raw:
        return None
    kept = AgentObjective.model_validate(raw)
    own = {k: v for k, v in kept.context.items() if k not in STRIPPED_CONTEXT}
    return kept.model_copy(update={"context": own})


def agent_run_service(factory: sessionmaker[Session]) -> RunAgentFn:
    """Run the task's agent for a workflow run in a per-run worktree (#250).

    The agent run's id derives from the workflow run and the activity's idempotency
    key, so a repeated call finds the row the first created and returns its recorded
    result instead of running the agent again. The branch is not pushed: the workflow
    opens the pull request in its own activity, after its checks.
    """

    def run(inp: RunAgentInput) -> AgentRunResultDTO:
        agent_run_id = uuid.uuid5(inp.workflow_run_id, inp.idempotency_key)
        with factory() as session:
            workflow_run = _owned_run(session, inp.workflow_run_id, inp.workspace_id)
            row = session.get(AgentRun, agent_run_id)
            if row is not None:
                recorded = AgentRunResult.model_validate(row.output or {})
                if recorded.status == ResultStatus.PENDING:  # an earlier call never finished
                    recorded.status, recorded.error = ResultStatus.FAILED, "run did not finish"
                return _workflow_result(agent_run_id, recorded)
            task = session.get(Task, workflow_run.task_id)
            assert task is not None  # workflow_run.task_id is a non-null foreign key
            kept = _kept_objective(workflow_run.context)
            target = AgentObjective(
                objective=task.title, repo_targets=task.repo_targets
            ).primary_repo_target
            if task.execution_mode == ExecutionMode.SUPERVISED_MULTI_AGENT:
                # The supervisor runs only on the Celery path; refuse rather than run one agent.
                raise ApplicationError(
                    f"task {task.id} runs supervised multi-agent, which forge.run_agent "
                    "does not support yet (ADR-0007)",
                    type="ExecutionModeNotSupported",
                    non_retryable=True,
                )
            if target is not None and not repo_connected(session, inp.workspace_id, target.repo):
                raise ApplicationError(
                    f"{target.repo} is not connected to workspace {inp.workspace_id}",
                    type="RepoNotConnected",
                    non_retryable=True,
                )
            objective = objective_for_task(
                task, agent_run_id=agent_run_id, repo_full_name=target.repo if target else None
            )
            if kept is not None:
                # The run keeps the text built from the re-fetched issue (#171). The
                # task still sets what the agent may do and where: its allowed and
                # restricted actions and its repository.
                objective = objective.model_copy(
                    update={
                        "key": kept.key or objective.key,
                        "objective": kept.objective,
                        "description": kept.description,
                        "acceptance_criteria": kept.acceptance_criteria,
                        "context": {**kept.context, **objective.context},
                    }
                )
            objective.context["workflow_run_id"] = str(inp.workflow_run_id)
            source = (workflow_run.context.get("trigger") or {}).get("source_label")
            if kept is not None and source:
                # Issue text is untrusted (ADR-0014): the prompt fences it under its source.
                objective.context["source"] = source
            failing = _failing_checks(workflow_run.context, inp.attempt)
            if failing:
                # The builder's next attempt sees what failed last time (#172).
                objective.context["failing_checks"] = failing
            session.add(
                AgentRun(
                    id=agent_run_id,
                    workspace_id=inp.workspace_id,
                    workflow_run_id=inp.workflow_run_id,
                    task_id=task.id,
                    skill_profile=task.skill_profile,
                    inputs=objective.model_dump(mode="json"),
                    status=RunStatus.PENDING,
                )
            )
            session.commit()
        result, _ = run_agent_objective(objective, deliver=False)
        return _workflow_result(agent_run_id, result)

    return run


def notify_service(factory: sessionmaker[Session]) -> NotifyFn:
    """Record that the workflow paused the run for a person (#250).

    The pause is a ``thread.state`` event (``awaiting_input``) on the workflow run's
    latest agent run, once per idempotency key. With no agent run there is nowhere
    to record it, so the activity reports ``False`` rather than an effect it did not
    perform.
    """

    def notify(inp: NotifyInput) -> bool:
        with factory() as session:
            _owned_run(session, inp.workflow_run_id, inp.workspace_id)
            agent_run_id = session.scalar(
                select(AgentRun.id)
                .where(
                    AgentRun.workflow_run_id == inp.workflow_run_id,
                    AgentRun.workspace_id == inp.workspace_id,
                )
                .order_by(AgentRun.created_at.desc())
                .limit(1)
            )
            if agent_run_id is None:
                return False
            recorded = session.scalars(
                select(AgentRunEvent.payload).where(
                    AgentRunEvent.run_id == agent_run_id,
                    AgentRunEvent.type == RunEventType.THREAD_STATE.value,
                )
            )
            if any(p.get("idempotency_key") == inp.idempotency_key for p in recorded):
                return True  # a retry of a pause already recorded
        sink = RunEventSink(
            agent_run_id, inp.workspace_id, session_factory=factory, redact=redact_text
        )
        return sink.emit(
            RunEventDraft(
                type=RunEventType.THREAD_STATE,
                summary=inp.reason,
                payload={
                    "state": "awaiting_input",
                    "kind": inp.kind,
                    "idempotency_key": inp.idempotency_key,
                },
            )
        )

    return notify


def _latest_agent_run(
    session: Session, workflow_run_id: uuid.UUID, workspace_id: uuid.UUID
) -> AgentRun | None:
    return session.scalars(
        select(AgentRun)
        .where(AgentRun.workflow_run_id == workflow_run_id, AgentRun.workspace_id == workspace_id)
        .order_by(AgentRun.created_at.desc())
        .limit(1)
    ).first()


def run_checks_service(factory: sessionmaker[Session]) -> RunChecksFn:
    """Run the repository's declared checks on the agent's commit (#251).

    The commit is the head the latest agent run recorded, so a retry checks the same
    code even if the run branch moved. The commands come from the default branch's
    ``.forge/policy.yaml``, as the pull-request gate's review rules do, so an agent
    cannot change its own checks. A repository that declares none fails.
    """

    def run(inp: RunChecksInput) -> ChecksResult:
        with factory() as session:
            workflow_run = _owned_run(session, inp.workflow_run_id, inp.workspace_id)
            agent_run = _latest_agent_run(session, inp.workflow_run_id, inp.workspace_id)
            artifacts = ((agent_run.output or {}).get("artifacts") or {}) if agent_run else {}
            head = artifacts.get("head_commit")
            task = session.get(Task, workflow_run.task_id)
            targets = task.repo_targets if task is not None else []
        if agent_run is None or not head:
            raise ApplicationError(
                f"workflow run {inp.workflow_run_id} has no agent run with a commit to check",
                type="AgentRunNotFound",
                non_retryable=True,
            )
        target = AgentObjective(objective="checks", repo_targets=targets).primary_repo_target
        assert target is not None  # the agent run resolved this repository
        objective = AgentObjective(
            objective="checks",
            context={"repo_full_name": target.repo, "workspace_id": str(inp.workspace_id)},
        )
        mirror = Path(str(resolve_run_repository(objective).context["repo_path"]))
        try:
            policy = default_branch_policy(mirror)
        except (ValueError, yaml.YAMLError):
            policy = None  # an unreadable policy declares no command: the checks fail
        runs = run_declared_checks(
            mirror, str(head), policy, run_id=agent_run.id, workspace_id=inp.workspace_id
        )
        _record_checks(factory, inp, head=str(head), runs=runs)
        if not runs:
            return ChecksResult(results=dict(NO_DECLARED_CHECKS))
        return ChecksResult(results={run.id: run.passed for run in runs})

    return run


#: The run-context key holding each check attempt's evidence (#172).
CHECKS_KEY = "checks"


def _record_checks(
    factory: sessionmaker[Session], inp: RunChecksInput, *, head: str, runs: list[CheckRun]
) -> None:
    """Record one check attempt on the workflow run, replacing a retry of the same one.

    Each check keeps its command id, exit status, start time, duration and a redacted
    tail of its output; the attempt keeps the commit it checked. ``GET
    /workflow/runs/{id}`` returns them with the run's context.
    """
    attempt = {
        "attempt": inp.attempt,
        "head_commit": head,
        "no_checks_declared": not runs,
        "checks": [
            {
                "id": run.id,
                "exit_code": run.exit_code,
                "timed_out": run.timed_out,
                "passed": run.passed,
                "started_at": run.started_at.isoformat(),
                "duration_ms": run.duration_ms,
                "output_tail": redact_text(run.output_tail),
            }
            for run in runs
        ],
    }
    with factory() as session:
        workflow_run = _owned_run(session, inp.workflow_run_id, inp.workspace_id)
        earlier = [
            a for a in workflow_run.context.get(CHECKS_KEY, []) if a.get("attempt") != inp.attempt
        ]
        workflow_run.context = {**workflow_run.context, CHECKS_KEY: [*earlier, attempt]}
        session.commit()


def _failing_checks(context: dict[str, Any], before_attempt: int) -> list[dict[str, Any]]:
    """The checks that failed in the attempt just before ``before_attempt`` (#172)."""
    attempts = [a for a in context.get(CHECKS_KEY, []) if a.get("attempt", 0) < before_attempt]
    if not attempts:
        return []
    last = max(attempts, key=lambda a: a.get("attempt", 0))
    if last.get("no_checks_declared"):
        return [{"id": "no_checks_declared", "exit_code": None, "output_tail": ""}]
    return [
        {"id": c["id"], "exit_code": c["exit_code"], "output_tail": c["output_tail"]}
        for c in last.get("checks", [])
        if not c.get("passed")
    ]


def open_pr_service(
    factory: sessionmaker[Session],
    *,
    credentials: Callable[[], GitHubAppCredentials | None] = GitHubAppCredentials.from_env,
    transport: httpx.BaseTransport | None = None,
    push: Pusher = git_push,
) -> OpenPrFn:
    """Push the run branch and open its pull request, exactly once (#252).

    The branch comes from the workspace's mirror, where the agent committed it. A
    retry finds the pull request the first attempt opened and returns it. Without
    GitHub App credentials the activity refuses: nothing can be delivered.
    """

    def open_pr(inp: OpenPrInput) -> OpenPrResult:
        with factory() as session:
            workflow_run = _owned_run(session, inp.workflow_run_id, inp.workspace_id)
            task = session.get(Task, workflow_run.task_id)
            agent_run = _latest_agent_run(session, inp.workflow_run_id, inp.workspace_id)
            artifacts = ((agent_run.output or {}).get("artifacts") or {}) if agent_run else {}
        creds = credentials()
        if creds is None:
            raise ApplicationError(
                "no GitHub App credentials on this worker: the pull request cannot be opened",
                type="GitHubAppNotConfigured",
                non_retryable=True,
            )
        branch = inp.branch_name or artifacts.get("branch")
        assert task is not None  # workflow_run.task_id is a non-null foreign key
        target = AgentObjective(
            objective=task.title, repo_targets=task.repo_targets
        ).primary_repo_target
        if not branch or target is None:
            raise ApplicationError(
                f"workflow run {inp.workflow_run_id} has no run branch to deliver",
                type="AgentRunNotFound",
                non_retryable=True,
            )
        owner, name = split_repo_name(target.repo)
        mirror = workspace_mirrors(inp.workspace_id) / owner / name
        if _commits_ahead(mirror, target.base_branch, str(branch)) == 0:
            # The Celery finalizer's ``no_changes``: GitHub refuses an empty pull request.
            raise ApplicationError(
                f"{branch} has no commits ahead of {target.base_branch}: nothing to deliver",
                type="NoChangesToDeliver",
                non_retryable=True,
            )
        title = f"{task.key + ': ' if task.key else ''}{task.title}"[:120]
        body = (
            f"Opened by SCF workflow run `{inp.workflow_run_id}` for task {task.key or task.id}."
            "\n\nMerging requires human approval through the SCF `pr` gate."
        )
        pr = push_and_open_pr(
            repo_path=mirror,
            branch=str(branch),
            repo_full_name=target.repo,
            base_branch=target.base_branch,
            title=title,
            body=body,
            credentials=creds,
            transport=transport,
            push=push,
            reuse_existing=True,
        )
        return OpenPrResult(pr_number=pr.number, pr_url=pr.url)

    return open_pr
