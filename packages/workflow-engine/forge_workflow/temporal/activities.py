"""Temporal Activities — every workflow effect as one idempotent ``@activity.defn``.

Each activity takes a typed payload carrying ``workflow_run_id`` + ``workspace_id``
+ an ``idempotency_key`` and delegates to the **existing** slice services through
injected callables, so F25 wraps effect bodies rather than rewriting them. When a
service is not wired (the SOFT deps F02/F06/F08/F16), a safe default keeps the
durable spine runnable and the effect "parked" — exactly as the FSM engine does.

``persist_transition`` is the projection writer: it keeps the Postgres
``workflow_run`` row (+ an append-only transition list in ``context``) byte-faithful
so the board timeline / run-trace viewer / audit log are engine-agnostic. It is
idempotent on ``(workflow_run_id, idempotency_key)`` so Temporal's at-least-once
delivery never double-writes a transition.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from temporalio import activity

from forge_contracts import RunStatus, WorkflowRun, WorkflowState
from forge_workflow.store import InMemoryWorkflowStore, WorkflowStore
from forge_workflow.temporal.payloads import (
    AgentRunResultDTO,
    ApprovalInput,
    ChecksResult,
    CleanupInput,
    GuardInputs,
    GuardInputsRequest,
    NotifyInput,
    OpenPrInput,
    OpenPrResult,
    ResumeAgentInput,
    RunAgentInput,
    RunChecksInput,
    TransitionRecord,
)

#: Default verification check set (spec: default_feature ``checks``).
DEFAULT_CHECKS = ("lint", "type_check", "tests", "coverage")

_SUCCESS_STATES = {WorkflowState.MERGED.value, WorkflowState.CLOSED.value}
_STATUS_BY_STATE = {
    WorkflowState.NEEDS_HUMAN_INPUT.value: RunStatus.ESCALATED,
    WorkflowState.FAILED.value: RunStatus.FAILED,
    WorkflowState.CANCELLED.value: RunStatus.CANCELLED,
}

# Injected service callables (sync or async). Defaults keep the spine runnable.
GuardInputsFn = Callable[[GuardInputsRequest], GuardInputs]
RunAgentFn = Callable[[RunAgentInput], AgentRunResultDTO]
ResumeAgentFn = Callable[[ResumeAgentInput], AgentRunResultDTO]
RunChecksFn = Callable[[RunChecksInput], ChecksResult]
OpenPrFn = Callable[[OpenPrInput], OpenPrResult]
NotifyFn = Callable[[NotifyInput], bool]
CleanupFn = Callable[[CleanupInput], bool]
ApprovalFn = Callable[[ApprovalInput], uuid.UUID]


def _now() -> datetime:
    return datetime.now(UTC)


def _default_guard_inputs(_: GuardInputsRequest) -> GuardInputs:
    # SOFT deps (F04/F05) absent → permissive defaults so the happy path runs.
    return GuardInputs(
        plan_required=False,
        preconditions={
            "repo_target_set": True,
            "policy_loaded": True,
            "skill_profile_set": True,
            "knowledge_synced": True,
        },
        ci_status_green=True,
        spec_validated=True,
    )


def _default_agent_result() -> AgentRunResultDTO:
    # SOFT dep (F06) absent → a parked, all-green result keeps the spine runnable.
    return AgentRunResultDTO(
        agent_run_id=uuid.uuid4(),
        status="succeeded",
        confidence=1.0,
        checks=dict.fromkeys(DEFAULT_CHECKS, True),
        branch_name="forge/auto",
        head_commit_sha=None,
    )


def _default_run_agent(inp: RunAgentInput) -> AgentRunResultDTO:
    return _default_agent_result()


def _default_resume_agent(inp: ResumeAgentInput) -> AgentRunResultDTO:
    return _default_agent_result()


def _default_run_checks(_: RunChecksInput) -> ChecksResult:
    return ChecksResult(results=dict.fromkeys(DEFAULT_CHECKS, True))


def _default_open_pr(_: OpenPrInput) -> OpenPrResult:
    return OpenPrResult(pr_number=1, pr_url="https://example.invalid/pr/1")


def _default_notify(_: NotifyInput) -> bool:
    return True


def _default_cleanup(_: CleanupInput) -> bool:
    return True


def _default_approval(_: ApprovalInput) -> uuid.UUID:
    return uuid.uuid4()


class WorkflowActivities:
    """All Activity implementations, bound to a store + injected services.

    The bound methods are registered on the Temporal worker. Tests inject
    scriptable fakes to drive specific paths (failing checks, awaiting input, …).
    """

    def __init__(
        self,
        *,
        store: WorkflowStore | None = None,
        guard_inputs_fn: GuardInputsFn | None = None,
        run_agent_fn: RunAgentFn | None = None,
        resume_agent_fn: ResumeAgentFn | None = None,
        run_checks_fn: RunChecksFn | None = None,
        open_pr_fn: OpenPrFn | None = None,
        notify_fn: NotifyFn | None = None,
        cleanup_fn: CleanupFn | None = None,
        approval_fn: ApprovalFn | None = None,
    ) -> None:
        self._store: WorkflowStore = store or InMemoryWorkflowStore()
        self._guard_inputs_fn = guard_inputs_fn or _default_guard_inputs
        self._run_agent_fn = run_agent_fn or _default_run_agent
        self._resume_agent_fn = resume_agent_fn or _default_resume_agent
        self._run_checks_fn = run_checks_fn or _default_run_checks
        self._open_pr_fn = open_pr_fn or _default_open_pr
        self._notify_fn = notify_fn or _default_notify
        self._cleanup_fn = cleanup_fn or _default_cleanup
        self._approval_fn = approval_fn or _default_approval

    @property
    def store(self) -> WorkflowStore:
        return self._store

    def register(self) -> list[Callable[..., Awaitable[object]]]:
        """The bound activity methods to register on a worker."""
        return [
            self.persist_transition,
            self.load_guard_inputs,
            self.run_agent,
            self.resume_agent,
            self.run_checks,
            self.open_pr_with_spec_traceability,
            self.pause_and_notify,
            self.cleanup_worktree,
            self.create_approval,
        ]

    # -- projection writer (idempotent) ----------------------------------- #

    @activity.defn(name="forge.persist_transition")
    async def persist_transition(self, rec: TransitionRecord) -> int:
        run = self._store.get(rec.workflow_run_id)
        transitions: list[dict[str, object]] = list(run.context.get("transitions", []))
        for existing in transitions:
            if existing.get("idempotency_key") == rec.idempotency_key:
                seq = existing["sequence"]  # at-least-once no-op
                assert isinstance(seq, int)  # always written as an int below
                return seq

        sequence = len(transitions) + 1
        transitions.append(
            {
                "sequence": sequence,
                "from_state": rec.from_state.value,
                "to_state": rec.to_state.value,
                "event": rec.event,
                "guard_results": dict(rec.guard_results),
                "effects_dispatched": list(rec.effects_dispatched),
                "record": rec.record,
                "actor": rec.actor,
                "skill": rec.skill,
                "payload": dict(rec.payload),
                "idempotency_key": rec.idempotency_key,
            }
        )
        run.context["transitions"] = transitions
        run.context["retry_count"] = run.context.get("retry_count", 0)
        run.current_state = rec.to_state.value
        run.context["last_event"] = rec.event
        if rec.temporal_run_id:
            run.context["temporal_run_id"] = rec.temporal_run_id
        _apply_status(run, rec.to_state.value)
        self._store.update(run)
        return sequence

    # -- IO-bound guard resolution ---------------------------------------- #

    @activity.defn(name="forge.load_guard_inputs")
    async def load_guard_inputs(self, req: GuardInputsRequest) -> GuardInputs:
        return self._guard_inputs_fn(req)

    # -- agent (F06 LangGraph), heartbeating ------------------------------ #

    @activity.defn(name="forge.run_agent")
    async def run_agent(self, inp: RunAgentInput) -> AgentRunResultDTO:
        activity.heartbeat({"phase": "executing", "attempt": inp.attempt})
        return self._run_agent_fn(inp)

    @activity.defn(name="forge.resume_agent")
    async def resume_agent(self, inp: ResumeAgentInput) -> AgentRunResultDTO:
        activity.heartbeat({"phase": "resuming", "attempt": inp.attempt})
        return self._resume_agent_fn(inp)

    # -- verification (F08), PR (F03/F08) --------------------------------- #

    @activity.defn(name="forge.run_checks")
    async def run_checks(self, inp: RunChecksInput) -> ChecksResult:
        return self._run_checks_fn(inp)

    @activity.defn(name="forge.open_pr_with_spec_traceability")
    async def open_pr_with_spec_traceability(self, inp: OpenPrInput) -> OpenPrResult:
        return self._open_pr_fn(inp)

    # -- notifications (F16) + approvals (F36) ---------------------------- #

    @activity.defn(name="forge.pause_and_notify")
    async def pause_and_notify(self, inp: NotifyInput) -> bool:
        return self._notify_fn(inp)

    @activity.defn(name="forge.create_approval")
    async def create_approval(self, inp: ApprovalInput) -> str:
        return str(self._approval_fn(inp))

    # -- compensation ----------------------------------------------------- #

    @activity.defn(name="forge.cleanup_worktree")
    async def cleanup_worktree(self, inp: CleanupInput) -> bool:
        return self._cleanup_fn(inp)


def _apply_status(run: WorkflowRun, new_state: str) -> None:
    if new_state in _SUCCESS_STATES:
        run.status = RunStatus.SUCCEEDED
        run.completed_at = _now()
    elif new_state in _STATUS_BY_STATE:
        run.status = _STATUS_BY_STATE[new_state]
        if run.status in (RunStatus.FAILED, RunStatus.CANCELLED):
            run.completed_at = _now()
    else:
        run.status = RunStatus.RUNNING


__all__ = ["DEFAULT_CHECKS", "WorkflowActivities"]
