"""Real services for the Temporal workflow's activities (issue #170).

Each function here returns the callable ``WorkflowActivities`` invokes for one
activity. An activity with no service here keeps refusing (ADR-0007).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from temporalio.exceptions import ApplicationError

from scf_api.observability.redaction import redact_text
from scf_api.realtime.approval_events import create_published, decided, publish_sync, relay_url
from scf_api.services.agent_dispatch import STRIPPED_CONTEXT, repo_connected
from scf_api.services.attestation_service import AttestationService
from scf_api.services.held_out import record_result, scenarios_for_run
from scf_api.services.workflow_gate_hook import GATE_BLOCKED, STEP_SUBJECT, agent_actor_for_run
from scf_approval.models import ApprovalRequest as DomainApprovalRequest
from scf_approval.models import GateType
from scf_contracts import (
    AgentObjective,
    AgentRunResult,
    ExecutionMode,
    GatedChange,
    ModelClient,
    Policy,
    RiskReader,
)
from scf_contracts import RunStatus as ResultStatus
from scf_contracts.review import ReviewVerdict
from scf_contracts.run_context import (
    CHECKS_KEY,
    HELD_OUT_CHECK,
    HELD_OUT_KEY,
    INDEPENDENT_REVIEW_CHECK,
    PULL_REQUEST_KEY,
    REVIEWS_KEY,
)
from scf_contracts.run_events import RunEventDraft, RunEventType
from scf_db.models import (
    AgentRun,
    AgentRunEvent,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    Attestation,
    KnowledgeSource,
    SkillProfile,
    Task,
    User,
    WorkflowRun,
)
from scf_db.models.enums import ApprovalGate, RunStatus, UserRole
from scf_db.models.held_out import SCENARIO_APPROVED
from scf_db.models.runs import WorkflowStationVisit
from scf_db.models.traceability import TraceabilitySpecRollup
from scf_integrations.github import GitHubClient
from scf_policy.dependency_gate import RegistryClient
from scf_worker.agent_runner import _commits_ahead, configured_model, run_agent_objective
from scf_worker.approval_activity import build_approval_service, make_create_approval_fn
from scf_worker.diff_gates import PublicRegistry, diff_gate_checks
from scf_worker.dispatch import (
    RepositoryUnavailable,
    objective_for_task,
    resolve_run_repository,
    split_repo_name,
    workspace_mirrors,
)
from scf_worker.finalizer import (
    GitHubAppCredentials,
    Pusher,
    git_push,
    mint_token,
    push_and_open_pr,
    workspace_credentials,
)
from scf_worker.held_out_check import HOOKS_CHANGED, changed_test_hooks, run_scenario
from scf_worker.held_out_drafting import VERIFIER_ROLE
from scf_worker.red_team import RED_TEAM_CHECK, RED_TEAM_OFF, adversary_model, attack_change
from scf_worker.review import (
    REVIEWER_ROLE,
    ReviewerRefused,
    ensure_heterogeneous,
    review_change,
    reviewer_client,
    reviewer_model,
)
from scf_worker.review_rules import default_branch_policy, review_rules_stamp
from scf_worker.run_events import RunEventSink
from scf_worker.run_workspace import NO_DECLARED_CHECKS, CheckRun, run_declared_checks
from scf_worker.tasks.approvals import _expire_gate, _to_domain
from scf_workflow.red_team_gate import (
    RED_TEAM_REQUIRED,
    REDTEAM_BLOCKED,
    REDTEAM_NOT_RUN,
    ensure_red_team_verdict,
    red_team_passes,
)
from scf_workflow.temporal.activities import (
    ApprovalFn,
    GuardInputsFn,
    NotifyFn,
    OpenPrFn,
    RedTeamFn,
    RunAgentFn,
    RunChecksFn,
    StationGateFn,
)
from scf_workflow.temporal.ids import station_visit_id
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
    StationGateInput,
    StationGateResult,
)
from scf_workflow.temporal.station_workflow import ATTESTATION_MISSING

__all__ = [
    "agent_run_service",
    "approval_service",
    "guard_inputs_service",
    "notify_service",
    "open_pr_service",
    "open_station_gate_service",
    "read_station_gate_service",
    "red_team_service",
    "run_checks_service",
]

#: The run-context key naming the optional preconditions a workspace has not configured.
NOT_IN_USE_KEY = "preconditions_not_in_use"

logger = logging.getLogger(__name__)


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
    """Record the run's red-team verdict once per phase. No adversary is wired yet,
    so the row is ``not_run`` with its reason, as the V1 engine records (#174)."""

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


def _gate_refused(reason: str) -> ApplicationError:
    return ApplicationError(reason, type="StationGateRefused", non_retryable=True)


def _station_gate(session: Session, inp: StationGateInput) -> ApprovalRequest | None:
    """The visit's gate in any status; refuses a request on its subject it did not open.

    Only the worker opens ``step`` gates (``POST /approvals`` refuses them), so the
    visit's one request names its run, gate, state and visit, and the run's agent asked.
    """
    run = inp.workflow_run_id
    rows = session.scalars(
        select(ApprovalRequest).where(
            ApprovalRequest.workspace_id == inp.workspace_id,
            ApprovalRequest.subject_type == STEP_SUBJECT,
            ApprovalRequest.subject_id == station_visit_id(run, inp.state, inp.visit),
        )
    ).all()
    ours = (run, inp.gate, agent_actor_for_run(run), {"run": str(run), "state": inp.state})
    for row in rows:
        payload = {k: row.payload.get(k) for k in ("run", "state")}
        named = (row.workflow_run_id, row.gate.value, row.requested_actor, payload)
        if named != ours or row.payload.get("visit") != inp.visit or len(rows) > 1:
            raise _gate_refused(f"approval request {row.id} is not the gate of this visit")
    return rows[0] if rows else None


def open_station_gate_service(factory: sessionmaker[Session]) -> StationGateFn:
    """Open a human station visit's approval request, once (spec K §4.2).

    A request the visit already has, in any status, is returned instead: the approval
    service reuses only a pending one, so a retry after a quick decision would open a
    second gate. A ``pr`` gate carries the repository's review rules, which may raise
    its quorum, as a feature run's does.
    """
    redis_url = relay_url()

    async def open_gate(inp: StationGateInput) -> StationGateResult:
        with factory() as session:
            run = _owned_run(session, inp.workflow_run_id, inp.workspace_id)
            row = _station_gate(session, inp)
            if row is not None:
                return StationGateResult(row.id, row.status.value)
            task = session.get(Task, run.task_id)
            task_id, project_id = run.task_id, task.project_id if task is not None else None
            targets = task.repo_targets if task is not None else []
        payload: dict[str, Any] = {
            "run": str(inp.workflow_run_id),
            "state": inp.state,
            "visit": inp.visit,
            "summary": inp.summary,
        }
        if inp.gate == GateType.PR.value:
            target = AgentObjective(objective="review rules", repo_targets=targets)
            if (repo := target.primary_repo_target) is not None:
                with factory() as session:  # only a repository this workspace connected
                    if not repo_connected(session, inp.workspace_id, repo.repo):
                        raise _gate_refused(f"{repo.repo} is not connected to this workspace")
                try:  # fetched as an agent run fetches it; review_rules_stamp needs the mirror
                    _mirror(inp.workspace_id, repo.repo, inp.workflow_run_id)
                except RepositoryUnavailable as exc:
                    raise _gate_refused(f"the pr gate cannot read {repo.repo}: {exc}") from exc
            payload |= review_rules_stamp(factory, workspace_id=inp.workspace_id, task_id=task_id)
        service = build_approval_service(factory)
        request = await create_published(
            service,
            redis_url=redis_url,
            workspace_id=inp.workspace_id,
            gate_type=GateType(inp.gate),
            subject_type=STEP_SUBJECT,
            subject_id=station_visit_id(inp.workflow_run_id, inp.state, inp.visit),
            workflow_run_id=inp.workflow_run_id,
            task_id=task_id,
            project_id=project_id,
            title=inp.summary or f"{inp.gate} approval for {inp.state}",
            requested_actor=agent_actor_for_run(inp.workflow_run_id),
            required_approvals=inp.required_approvals,
            expires_at=inp.expires_at,
            gate_payload=payload,
        )
        if _visit_cancelled(factory, inp):
            # Cancelled while this opened: the cancel's cleanup may have run before the
            # request existed. The visit is marked cancelled before that cleanup runs.
            await service.expire(request, actor="system", reason="run_cancelled")
            _announce_expired(redis_url, request)
        return StationGateResult(request.id, request.status.value)

    return open_gate


def _visit_cancelled(factory: sessionmaker[Session], inp: StationGateInput) -> bool:
    visit_id = station_visit_id(inp.workflow_run_id, inp.state, inp.visit)
    with factory() as session:
        return (
            session.scalar(
                select(WorkflowStationVisit.status).where(WorkflowStationVisit.id == visit_id)
            )
            == "cancelled"
        )


def _announce_expired(redis_url: str | None, request: DomainApprovalRequest) -> None:
    """Tell ``/ws`` clients the gate expired, as the SLA sweep does."""
    publish_sync(redis_url, decided(request, decided_by="system", surface="system"))


def _attested(session: Session, row: ApprovalRequest) -> bool | None:
    """Whether an approved ``pr`` gate's attestation is recorded for its run.

    ``None`` while the gate hook may still be recording it: the decision is written
    before the hook runs, and the hook stamps either the attestation's id or a block.
    """
    proof = row.payload.get("attestation_id")
    if proof is None:
        blocked = (row.payload.get("workflow_event") or {}).get("status") == GATE_BLOCKED
        return False if blocked else None
    try:
        attestation_id = uuid.UUID(str(proof))
    except ValueError:
        return False
    found = session.scalar(
        select(Attestation.id).where(
            Attestation.id == attestation_id,
            Attestation.workspace_id == row.workspace_id,
            Attestation.workflow_run_id == row.workflow_run_id,
        )
    )
    return found is not None


#: The vote that resolves a gate to each decided status.
_VOTE_FOR_STATUS = {
    ApprovalStatus.APPROVED: "approve",
    ApprovalStatus.REJECTED: "reject",
    ApprovalStatus.CHANGES_REQUESTED: "request_changes",
}


def _decider(session: Session, row: ApprovalRequest) -> str:
    """``user:<id>`` of whoever decided ``row``, once every voter is a person of its workspace.

    An agent (an agent-runner user), an outsider, or a status no vote stands behind (an
    automation or system write) refuses: it is never an outcome (ADR-0020).
    """
    votes: dict[uuid.UUID, str] = dict(
        session.execute(
            select(ApprovalDecision.approver_user_id, ApprovalDecision.decision).where(
                ApprovalDecision.approval_request_id == row.id
            )
        ).all()
    )
    people = set(
        session.scalars(
            select(User.id).where(
                User.id.in_(votes),
                User.workspace_id == row.workspace_id,
                User.role != UserRole.AGENT_RUNNER,
            )
        )
    )
    decider = row.decided_by_id
    vote = votes.get(decider) if decider is not None else None
    if vote is None or vote != _VOTE_FOR_STATUS.get(row.status) or people != set(votes):
        raise _gate_refused(f"approval request {row.id} was not decided by a person")
    return f"user:{decider}"


def read_station_gate_service(factory: sessionmaker[Session]) -> StationGateFn:
    """Read a human station visit's gate and its decisions (spec K §4.2, ADR-0020).

    A decision is the outcome only on this visit's request and only when a person of
    the run's workspace made it; anything else refuses, so the station fails rather
    than move on a decision nobody may make. An approved ``pr`` gate counts only once
    the attestation for it is recorded; without one the outcome is
    ``attestation_missing``, which parks the run (fail closed, as feature runs block).
    Past the deadline (``expire``) a gate still pending expires with the SLA sweep's
    conditional update, which never overwrites a decision that landed first, and the
    expiry is announced as the sweep announces it.
    """
    redis_url = relay_url()

    def read(inp: StationGateInput) -> StationGateResult:
        with factory() as session:
            _owned_run(session, inp.workflow_run_id, inp.workspace_id)
            row = _station_gate(session, inp)
            if row is None:
                raise _gate_refused(f"{inp.state}#{inp.visit} has no gate")
            if row.status is ApprovalStatus.PENDING and inp.expire:
                if _expire_gate(session, row, datetime.now(UTC)):
                    session.commit()
                    _announce_expired(redis_url, _to_domain(row))
                    return StationGateResult(row.id, ApprovalStatus.EXPIRED.value)
                session.refresh(row)  # decided since it was read
            if row.status in (ApprovalStatus.PENDING, ApprovalStatus.EXPIRED):
                return StationGateResult(row.id, row.status.value)
            actor = _decider(session, row)
            if row.gate is ApprovalGate.PR and row.status is ApprovalStatus.APPROVED:
                attested = _attested(session, row)
                if attested is None and not inp.expire:
                    return StationGateResult(row.id, ApprovalStatus.PENDING.value)
                if not attested:
                    return StationGateResult(row.id, ATTESTATION_MISSING, actor=actor)
            return StationGateResult(row.id, row.status.value, actor=actor)

    return read


def _mirror(workspace_id: uuid.UUID, repo: str, run_id: uuid.UUID) -> Path:
    """The workspace's mirror of ``repo``, fetched first when GitHub App credentials exist.

    Raises :class:`RepositoryUnavailable` with no credentials and no mirror.
    """
    objective = AgentObjective(
        objective="mirror",
        context={
            "repo_full_name": repo,
            "workspace_id": str(workspace_id),
            "workflow_run_id": str(run_id),
        },
    )
    return Path(str(resolve_run_repository(objective).context["repo_path"]))


def _policy_loads(workspace_id: uuid.UUID, repo: str, run_id: uuid.UUID) -> bool:
    """The default branch's policy file parses, or there is none and the default applies.

    The mirror is fetched first when GitHub App credentials exist, so the file is the
    default branch's current one; with no credentials and no mirror nothing is readable.
    """
    try:
        mirror = _mirror(workspace_id, repo, run_id)
    except RepositoryUnavailable:
        return False
    try:
        default_branch_policy(mirror)
    except (ValueError, yaml.YAMLError):  # a git failure raises on, so the activity retries
        return False
    return True


#: The merge guard's CI read: check runs on the one repository, nothing else (#280).
CI_TOKEN_PERMISSIONS: dict[str, str] = {"checks": "read"}
#: Completed conclusions that do not fail CI; any other (failure, cancelled,
#: timed_out, action_required, stale, startup_failure) does.
_CI_PASSING = frozenset({"success", "neutral", "skipped"})
#: The run-context key recording the last merge guard read (FR-011, FR-012).
MERGE_GUARD_KEY = "merge_guard"


class CiUnreadable(RuntimeError):
    """CI could not be read for the run; it does not count as green."""


def github_check_runs(
    repo: str,
    ref: str,
    *,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    session_factory: sessionmaker[Session],
) -> list[dict[str, Any]]:
    """The check runs on commit ``ref``, read with the workspace's token for ``repo`` alone."""
    split_repo_name(repo)
    creds = workspace_credentials(workspace_id, repo, session_factory=session_factory)
    if creds is None:
        raise CiUnreadable("no GitHub App credentials for this workspace")
    token = mint_token(
        creds,
        repo,
        CI_TOKEN_PERMISSIONS,
        workspace_id=workspace_id,
        run_id=run_id,
        session_factory=session_factory,
    )
    client = GitHubClient(token=token, base_url=creds.api_base_url)
    try:
        return client.check_runs_for_ref(repo, ref)
    finally:
        client.close()


def ci_verdict(runs: list[dict[str, Any]]) -> tuple[bool, str]:
    """Green only when there are check runs and every one completed and passed."""
    if not runs:
        return False, "ci_no_check_runs"
    pending = sorted(str(r.get("name")) for r in runs if r.get("status") != "completed")
    if pending:  # read once per approval: gates/pr/sync re-reads it later
        return False, f"ci_pending: {', '.join(pending)}"
    failed = sorted(str(r.get("name")) for r in runs if r.get("conclusion") not in _CI_PASSING)
    if failed:
        return False, f"ci_failed: {', '.join(failed)}"
    return True, "ci_green"


def _spec_validated(session: Session, task: Task) -> tuple[bool, str]:
    """The spec engine's validation of the task's spec; true when the run has none."""
    if task.spec_id is None:
        return True, "no_spec"  # an issue-triggered run carries no drafted spec
    rollup = session.scalar(
        select(TraceabilitySpecRollup).where(
            TraceabilitySpecRollup.workspace_id == task.workspace_id,
            TraceabilitySpecRollup.spec_id == task.spec_id,
        )
    )
    # The rollup's verdict (scf_spec ValidationStatus): "passing" when every criterion
    # is validated against the current spec version; partial, failing, stale or none
    # otherwise.
    return rollup is not None and rollup.validation_status == "passing", "spec_rollup"


def _merge_guard(factory: sessionmaker[Session], req: GuardInputsRequest) -> GuardInputs:
    """CI on the agent's head commit and the spec's validation, read by SCF (#280).

    A caller's payload never decides these. Unreadable CI is not green: the person
    re-applies the approval (``gates/pr/sync``), which reads CI again.
    """
    with factory() as session:
        run = _owned_run(session, req.workflow_run_id, req.workspace_id)
        task = session.get(Task, run.task_id)
        assert task is not None  # workflow_run.task_id is a non-null foreign key
        target = AgentObjective(
            objective="merge guard", repo_targets=task.repo_targets
        ).primary_repo_target
        builder = _latest_agent_run(session, req.workflow_run_id, req.workspace_id)
        artifacts = ((builder.output or {}).get("artifacts") or {}) if builder else {}
        head = artifacts.get("head_commit")
        spec_validated, spec_source = _spec_validated(session, task)
    if target is None or not head:
        green, ci_reason = False, "ci_unreadable: the run has no repository or head commit"
    else:
        try:
            runs = github_check_runs(
                target.repo,
                str(head),
                workspace_id=req.workspace_id,
                run_id=req.workflow_run_id,
                session_factory=factory,
            )
            green, ci_reason = ci_verdict(runs)
        except Exception as exc:  # an unreadable CI blocks the merge; it never fails the run
            # GitHub's own message names the status (for example a missing checks:read).
            logger.warning("CI could not be read for workflow run %s", req.workflow_run_id)
            green, ci_reason = False, f"ci_unreadable: {type(exc).__name__}: {exc}"[:500]
    record = {
        "head_commit": head,
        "ci_status_green": green,
        "ci_reason": ci_reason,
        "spec_validated": spec_validated,
        "spec_source": spec_source,
        "read_at": datetime.now(UTC).isoformat(),
    }
    with factory() as session:
        run = _owned_run(session, req.workflow_run_id, req.workspace_id)
        run.context = {**run.context, MERGE_GUARD_KEY: record}
        session.commit()
    return GuardInputs(plan_required=True, ci_status_green=green, spec_validated=spec_validated)


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
        if req.phase == "merge":
            return _merge_guard(factory, req)
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
            and _policy_loads(req.workspace_id, target.repo, req.workflow_run_id),
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
        if inp.station_visit_id is not None:
            # shortcut: no station agent body yet (role tools, objective, token cap,
            # decision), so a line's agent station must not run the task's agent; bind
            # one here when station agents ship. The station reads ``not_configured``.
            raise ApplicationError(
                "forge.run_agent does not serve line station inputs on this worker yet (ADR-0007)",
                type="ActivityNotConfigured",
                non_retryable=True,
            )
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
            held_out = (_last_attempt(workflow_run.context, inp.attempt) or {}).get(HELD_OUT_KEY)
            if held_out and held_out.get("failed"):
                # Of the held-out scenarios, only how many failed (#286).
                objective.context["held_out_failed"] = held_out["failed"]
            session.add(
                AgentRun(
                    id=agent_run_id,
                    workspace_id=inp.workspace_id,
                    workflow_run_id=inp.workflow_run_id,
                    task_id=task.id,
                    skill_profile=task.skill_profile,
                    inputs=objective.model_dump(mode="json"),
                    status=RunStatus.PENDING,
                    # The independent reviewer must be of another family (#173).
                    model=configured_model(),
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
            builder = _latest_agent_run(session, inp.workflow_run_id, inp.workspace_id)
            if builder is None:
                return False
            agent_run_id = builder.id
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
    """The builder's latest run: a reviewer's (#173) or verifier's (#258) holds no commit."""
    return session.scalars(
        select(AgentRun)
        .where(
            AgentRun.workflow_run_id == workflow_run_id,
            AgentRun.workspace_id == workspace_id,
            AgentRun.role.notin_((REVIEWER_ROLE, VERIFIER_ROLE)),
        )
        .order_by(AgentRun.created_at.desc())
        .limit(1)
    ).first()


def run_checks_service(
    factory: sessionmaker[Session],
    *,
    reviewer: Callable[[str], ModelClient] = reviewer_client,
    adversary: Callable[[str], ModelClient] = reviewer_client,
    registry: RegistryClient | None = None,
    risk: RiskReader | None = None,
) -> RunChecksFn:
    """Run the repository's declared checks on the agent's commit (#251).

    The commit is the head the latest agent run recorded, so a retry checks the same
    code even if the run branch moved. The commands come from the default branch's
    ``.forge/policy.yaml``, as the pull-request gate's review rules do, so an agent
    cannot change its own checks. A repository that declares none fails. Beside them
    run the gates on the diff (#187): ``secret_scan``, ``dependency_check`` (asking
    ``registry``, PyPI and npm by default, about added dependencies) and ``sast``.

    When every check passes, an independent reviewer judges the change (#173): its
    verdict is the check ``independent_review``. When it approves, an adversary
    attacks the change (#283): its verdict is the check ``red_team``. With no
    reviewer or adversary it can use, the result asks the workflow to pause for a
    person instead; so does a scan the change's enforced risk class requires and
    the workspace turned off (#194, read through ``risk``). When all of that passes,
    the run's approved held-out scenarios run on the change (#286): the check
    ``held_out_scenarios``.
    """

    def run(inp: RunChecksInput) -> ChecksResult:
        with factory() as session:
            workflow_run = _owned_run(session, inp.workflow_run_id, inp.workspace_id)
            agent_run = _latest_agent_run(session, inp.workflow_run_id, inp.workspace_id)
            artifacts = ((agent_run.output or {}).get("artifacts") or {}) if agent_run else {}
            head = artifacts.get("head_commit")
            task = session.get(Task, workflow_run.task_id)
            assert task is not None  # workflow_run.task_id is a non-null foreign key
            targets = task.repo_targets
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
            context={
                "repo_full_name": target.repo,
                "workspace_id": str(inp.workspace_id),
                "workflow_run_id": str(inp.workflow_run_id),
            },
        )
        mirror = Path(str(resolve_run_repository(objective).context["repo_path"]))
        try:
            policy = default_branch_policy(mirror)
        except (ValueError, yaml.YAMLError):
            policy = None  # an unreadable policy declares no command: the checks fail
        runs = run_declared_checks(
            mirror, str(head), policy, run_id=agent_run.id, workspace_id=inp.workspace_id
        )
        gates_pause: str | None = None  # a gate the worker could not run asks a person
        if runs and policy is not None:  # none declared still fails as no_checks_declared
            gates, gates_pause = diff_gate_checks(
                mirror,
                base=target.base_branch,
                head=str(head),
                rules=policy.diff_gates,
                registry=registry or PublicRegistry(),
            )
            runs += gates
        if not runs or not all(r.passed for r in runs):
            _record_checks(factory, inp, head=str(head), runs=runs)
            results = {r.id: r.passed for r in runs} or dict(NO_DECLARED_CHECKS)
            return ChecksResult(results=results, pause_reason=gates_pause)
        try:
            review = _independent_review(
                factory,
                inp,
                mirror=mirror,
                base=target.base_branch,
                head=str(head),
                builder=agent_run,
                project_id=task.project_id,
                reviewer=reviewer,
            )
        except ReviewerRefused as refused:
            _record_checks(factory, inp, head=str(head), runs=runs)
            _record_review(factory, inp, {"refused": refused.reason, "detail": str(refused)})
            return ChecksResult(results={r.id: r.passed for r in runs}, pause_reason=refused.reason)
        runs = [*runs, review]
        pause = None
        if review.passed:
            scan, pause = _red_team(
                factory,
                inp,
                mirror=mirror,
                base=target.base_branch,
                head=str(head),
                builder=agent_run,
                task=task,
                policy=policy,
                adversary=adversary,
                risk=risk,
            )
            runs += [scan] if scan is not None else []
        held_out: dict[str, Any] | None = None
        if pause is None and all(r.passed for r in runs):
            hidden, held_out, pause = _held_out(
                factory,
                inp,
                mirror=mirror,
                base=target.base_branch,
                head=str(head),
                builder=agent_run,
                task=task,
                policy=policy,
            )
            runs += [hidden] if hidden is not None else []
        _record_checks(factory, inp, head=str(head), runs=runs, held_out=held_out)
        return ChecksResult(results={r.id: r.passed for r in runs}, pause_reason=pause)

    return run


def _held_out(
    factory: sessionmaker[Session],
    inp: RunChecksInput,
    *,
    mirror: Path,
    base: str,
    head: str,
    builder: AgentRun,
    task: Task,
    policy: Policy | None,
) -> tuple[CheckRun | None, dict[str, Any], str | None]:
    """Run the run's approved held-out scenarios on the head commit, as one check (#286).

    Returns the check ``held_out_scenarios`` (none when no scenario ran), the counts
    the attempt records (``none_defined`` or ``none_approved`` when none is approved),
    and the reason to pause for a person, if any. A change that touches a file hooking
    into the test run could make any scenario pass, so then none runs and the run
    pauses, naming the files; the builder is not told (sec_001). Each outcome is
    audited by scenario id (``held_out.ran``) as it finishes, never put on the run,
    whose context any reader sees.
    """
    started_at = datetime.now(UTC)
    with factory() as session:
        scenarios = scenarios_for_run(
            session,
            workspace_id=inp.workspace_id,
            task_id=task.id,
            workflow_run_id=inp.workflow_run_id,
        )
    approved = [s for s in scenarios if s.state == SCENARIO_APPROVED]
    counts = {"defined": len(scenarios), "passed": 0, "failed": 0}
    if not approved:
        return None, {"verdict": "none_approved" if scenarios else "none_defined", **counts}, None
    hooks = changed_test_hooks(mirror, base, head, [s.command for s in approved])
    if hooks:
        named = ", ".join(hooks[:10]) + (f" and {len(hooks) - 10} more" if len(hooks) > 10 else "")
        return None, {"verdict": "could_not_verify", **counts}, f"{HOOKS_CHANGED}: {named}"
    for scenario in approved:
        outcome = run_scenario(
            mirror,
            head,
            files=scenario.files,
            command=scenario.command,
            policy=policy,
            run_id=builder.id,
            workspace_id=inp.workspace_id,
        )
        counts["passed" if outcome.passed else "failed"] += 1
        with factory() as session:  # now, so a later failure in the loop cannot lose it
            record_result(
                session,
                scenario,
                workflow_run_id=inp.workflow_run_id,
                attempt=inp.attempt,
                head_commit=head,
                outcome={**asdict(outcome), "passed": outcome.passed},
            )
            session.commit()
    failed = counts["failed"]
    return (
        CheckRun(
            id=HELD_OUT_CHECK,
            exit_code=1 if failed else 0,
            timed_out=False,
            started_at=started_at,
            duration_ms=int((datetime.now(UTC) - started_at).total_seconds() * 1000),
            output_tail=f"{failed} of {len(approved)} held-out scenarios failed",
        ),
        {"verdict": "failed" if failed else "passed", **counts},
        None,
    )


def _red_team(
    factory: sessionmaker[Session],
    inp: RunChecksInput,
    *,
    mirror: Path,
    base: str,
    head: str,
    builder: AgentRun,
    task: Task,
    policy: Policy | None,
    adversary: Callable[[str], ModelClient],
    risk: RiskReader | None,
) -> tuple[CheckRun | None, str | None]:
    """Attack the change with an adversary of another model family, once per head commit.

    Returns the check ``red_team`` and the reason to pause for a person, if any. A scan
    the workspace turned off is no check and no pause; the record says ``not_run``.
    When the change's repository enforces a class that requires red-team, only a
    survived scan lets it through (#194): an unrun one pauses for a person.
    """
    started_at = datetime.now(UTC)
    with factory() as session:
        model = adversary_model(session, workspace_id=inp.workspace_id, project_id=task.project_id)

        def scan(_: RedTeamInput) -> RedTeamResult:
            session.commit()  # hold no transaction open while the adversary runs
            return attack_change(
                mirror,
                base=base,
                head=head,
                criteria=list((builder.inputs or {}).get("acceptance_criteria") or []),
                builder_model=str(builder.model or ""),
                adversary=model,
                client=adversary,
                policy=policy,
                run_id=builder.id,
                workspace_id=inp.workspace_id,
            )

        row, _ = ensure_red_team_verdict(
            session,
            inp.workspace_id,
            workflow_run_id=inp.workflow_run_id,
            task_id=task.id,
            phase="pr",
            red_team_fn=scan,
            head_commit=head,
        )
        session.commit()
        verdict, kind, evidence = row.verdict, row.kind, dict(row.evidence)
        by = row.adversary_model
    reason = str(evidence.get("reason")) if verdict == REDTEAM_NOT_RUN else None
    own = verdict != REDTEAM_BLOCKED and reason in (None, RED_TEAM_OFF)
    change = GatedChange(
        workspace_id=inp.workspace_id, commit_sha=head, workflow_run_id=inp.workflow_run_id
    )
    decided = red_team_passes(verdict, passes=own, risk=risk, change=change)  # observed
    if own and not decided:
        return None, RED_TEAM_REQUIRED
    if verdict == REDTEAM_NOT_RUN:
        return None, (None if reason == RED_TEAM_OFF else reason)
    test = evidence.get("failing_test") or {}
    violation = evidence.get("violation") or {}
    summary = f"{verdict} ({kind}) by {by}"
    if test:
        summary += f": `{test['test_cmd']}` exited {test['exit_code']} ({test['path']})"
    if violation:
        summary += f": violates {violation['criterion_id']}"
    return CheckRun(
        id=RED_TEAM_CHECK,
        exit_code=1 if verdict == REDTEAM_BLOCKED else 0,
        timed_out=False,
        started_at=started_at,
        duration_ms=int((datetime.now(UTC) - started_at).total_seconds() * 1000),
        output_tail="\n".join([summary, str(evidence.get("output") or "")]),
    ), None


def _independent_review(
    factory: sessionmaker[Session],
    inp: RunChecksInput,
    *,
    mirror: Path,
    base: str,
    head: str,
    builder: AgentRun,
    project_id: uuid.UUID,
    reviewer: Callable[[str], ModelClient],
) -> CheckRun:
    """Judge the change with a reviewer of another model family, as one check (#173).

    The reviewer's run is an ``agent_run`` with role ``reviewer`` on the workflow run,
    its verdict in ``output``; a retry of the same attempt reuses a recorded verdict.
    """
    review_id = uuid.uuid5(inp.workflow_run_id, f"{inp.idempotency_key}:review")
    with factory() as session:
        row = session.get(AgentRun, review_id)
        recorded = (row.output or {}).get("verdict") if row is not None else None
        model = reviewer_model(session, workspace_id=inp.workspace_id, project_id=project_id)
    ensure_heterogeneous(builder.model, model)
    started_at = datetime.now(UTC)
    if recorded is not None:
        assert row is not None
        verdict = ReviewVerdict.model_validate(row.output)
    else:
        criteria = [
            f"{c.get('id')}: {c.get('text')}"
            for c in (builder.inputs or {}).get("acceptance_criteria") or []
        ]
        verdict, objective, result = review_change(
            mirror,
            base=base,
            head=head,
            criteria=criteria,
            client=reviewer(model),
            reviewer=model,
            builder_model=str(builder.model),
        )
        with factory() as session:
            session.merge(
                AgentRun(
                    id=review_id,
                    workspace_id=inp.workspace_id,
                    workflow_run_id=inp.workflow_run_id,
                    task_id=builder.task_id,
                    role=REVIEWER_ROLE,
                    model=model,
                    inputs=objective.model_dump(mode="json"),
                    output={
                        **verdict.model_dump(mode="json"),
                        "error": result.error,
                        "model_usage": result.artifacts.get("model_usage"),
                    },
                    status=RunStatus(result.status.value),
                )
            )
            session.commit()
    _record_review(
        factory, inp, {**verdict.model_dump(mode="json"), "agent_run_id": str(review_id)}
    )
    approved = verdict.verdict == "approve"
    return CheckRun(
        id=INDEPENDENT_REVIEW_CHECK,
        exit_code=0 if approved else 1,
        timed_out=False,
        started_at=started_at,
        duration_ms=int((datetime.now(UTC) - started_at).total_seconds() * 1000),
        output_tail=_findings_text(verdict),
    )


def _findings_text(verdict: ReviewVerdict) -> str:
    """The verdict as the builder's next attempt reads it."""
    head = verdict.verdict + (f" ({verdict.reason})" if verdict.reason else "")
    if verdict.truncated:
        head += "; the reviewer saw only the start of the diff"
    lines = [f"- {f.file}:{f.line} [{f.severity}] {f.statement}" for f in verdict.findings]
    return "\n".join([head, *lines])


def _record_review(
    factory: sessionmaker[Session], inp: RunChecksInput, review: dict[str, Any]
) -> None:
    """Record one attempt's review on the run, replacing a retry of the same one."""
    entry = {"attempt": inp.attempt, **review}
    with factory() as session:
        workflow_run = _owned_run(session, inp.workflow_run_id, inp.workspace_id)
        earlier = [
            r for r in workflow_run.context.get(REVIEWS_KEY, []) if r.get("attempt") != inp.attempt
        ]
        workflow_run.context = {**workflow_run.context, REVIEWS_KEY: [*earlier, entry]}
        session.commit()


def _record_checks(
    factory: sessionmaker[Session],
    inp: RunChecksInput,
    *,
    head: str,
    runs: list[CheckRun],
    held_out: dict[str, Any] | None = None,
) -> None:
    """Record one check attempt on the workflow run, replacing a retry of the same one.

    Each check keeps its command id, exit status, start time, duration and a redacted
    tail of its output; the attempt keeps the commit it checked and, once the held-out
    scenarios were reached, their counts (#286). ``GET /workflow/runs/{id}`` returns
    them with the run's context.
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
        **({HELD_OUT_KEY: held_out} if held_out is not None else {}),
    }
    with factory() as session:
        workflow_run = _owned_run(session, inp.workflow_run_id, inp.workspace_id)
        earlier = [
            a for a in workflow_run.context.get(CHECKS_KEY, []) if a.get("attempt") != inp.attempt
        ]
        workflow_run.context = {**workflow_run.context, CHECKS_KEY: [*earlier, attempt]}
        session.commit()


def _last_attempt(context: dict[str, Any], before_attempt: int) -> dict[str, Any] | None:
    """The check attempt just before ``before_attempt``, if any."""
    attempts = [a for a in context.get(CHECKS_KEY, []) if a.get("attempt", 0) < before_attempt]
    return max(attempts, key=lambda a: a.get("attempt", 0)) if attempts else None


def _failing_checks(context: dict[str, Any], before_attempt: int) -> list[dict[str, Any]]:
    """The checks that failed in the attempt just before ``before_attempt`` (#172).

    Never the held-out check: the builder learns only how many scenarios failed (#286).
    """
    last = _last_attempt(context, before_attempt)
    if last is None:
        return []
    if last.get("no_checks_declared"):
        return [{"id": "no_checks_declared", "exit_code": None, "output_tail": ""}]
    return [
        {"id": c["id"], "exit_code": c["exit_code"], "output_tail": c["output_tail"]}
        for c in last.get("checks", [])
        if not c.get("passed") and c["id"] != HELD_OUT_CHECK
    ]


def open_pr_service(
    factory: sessionmaker[Session],
    *,
    credentials: Callable[[uuid.UUID, str], GitHubAppCredentials | None] | None = None,
    transport: httpx.BaseTransport | None = None,
    push: Pusher = git_push,
) -> OpenPrFn:
    """Push the run branch and open its pull request, exactly once (#252).

    The branch comes from the workspace's mirror, where the agent committed it. A
    retry finds the pull request the first attempt opened and returns it. Without the
    workspace's GitHub App credentials (#186) the activity refuses: nothing can be
    delivered.
    """

    def open_pr(inp: OpenPrInput) -> OpenPrResult:
        with factory() as session:
            workflow_run = _owned_run(session, inp.workflow_run_id, inp.workspace_id)
            task = session.get(Task, workflow_run.task_id)
            agent_run = _latest_agent_run(session, inp.workflow_run_id, inp.workspace_id)
            artifacts = ((agent_run.output or {}).get("artifacts") or {}) if agent_run else {}
            reviews = [r for r in workflow_run.context.get(REVIEWS_KEY, []) if r.get("verdict")]
        branch = inp.branch_name or artifacts.get("branch")
        head = artifacts.get("head_commit")
        assert task is not None  # workflow_run.task_id is a non-null foreign key
        target = AgentObjective(
            objective=task.title, repo_targets=task.repo_targets
        ).primary_repo_target
        if not branch or not head or target is None:
            raise ApplicationError(
                f"workflow run {inp.workflow_run_id} has no run branch to deliver",
                type="AgentRunNotFound",
                non_retryable=True,
            )
        creds = (
            credentials(inp.workspace_id, target.repo)
            if credentials
            else workspace_credentials(inp.workspace_id, target.repo, session_factory=factory)
        )
        if creds is None:
            raise ApplicationError(
                "this workspace has no GitHub App credentials: the pull request cannot be opened",
                type="GitHubAppNotConfigured",
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
        if reviews:  # the verdict that let the change through (#173)
            review = max(reviews, key=lambda r: r.get("attempt", 0))
            body += (
                f"\n\nIndependent review: `{review['verdict']}` by "
                f"`{review.get('reviewer_model')}`; the builder ran on "
                f"`{review.get('builder_model')}`."
            )
        with factory() as session:
            # Signed before a person sees the pull request, once per head commit (#175).
            attestation = AttestationService(session).attest_pull_request(
                inp.workflow_run_id, repository=target.repo, head_commit=str(head)
            )
            session.commit()
            body += (
                f"\n\nAttestation `{attestation.id}`: payload sha256 "
                f"`{attestation.payload_hash}`, key `{attestation.keyid}`. Verify it with "
                f"`scf-verify --run {inp.workflow_run_id} --attestation-id {attestation.id}`."
            )
        pr = push_and_open_pr(
            repo_path=mirror,
            branch=str(branch),
            repo_full_name=target.repo,
            base_branch=target.base_branch,
            title=title,
            body=body,
            credentials=creds,
            workspace_id=inp.workspace_id,
            run_id=inp.workflow_run_id,
            session_factory=factory,
            transport=transport,
            push=push,
            reuse_existing=True,
        )
        with factory() as session:
            workflow_run = _owned_run(session, inp.workflow_run_id, inp.workspace_id)
            opened = {"number": pr.number, "url": pr.url}
            workflow_run.context = {**workflow_run.context, PULL_REQUEST_KEY: opened}
            session.commit()
        return OpenPrResult(pr_number=pr.number, pr_url=pr.url)

    return open_pr
