"""Temporal Activities — every workflow effect as one idempotent ``@activity.defn``.

Each activity takes a typed payload carrying ``workflow_run_id`` + ``workspace_id``
+ an ``idempotency_key`` and delegates to the **existing** slice services through
injected callables, so F25 wraps effect bodies rather than rewriting them.

Production paths fail closed (ADR-0007): an activity whose service is not
injected raises a non-retryable ``ActivityNotConfigured`` error. The stand-in
defaults (all-green agent result, passing checks, no-op notify/approval) run only
when the caller opts in with ``allow_fakes=True`` — tests, or a dev worker
started with ``FORGE_ALLOW_FAKE_ACTIVITIES=1``. The stand-ins never invent a PR.

``persist_transition`` is the projection writer: it keeps the Postgres
``workflow_run`` row (+ an append-only transition list in ``context``) byte-faithful
so the board timeline / run-trace viewer / audit log are engine-agnostic. It is
idempotent on ``(workflow_run_id, idempotency_key)`` so Temporal's at-least-once
delivery never double-writes a transition.
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import Executor, ThreadPoolExecutor
from datetime import UTC, datetime
from typing import NoReturn

from temporalio import activity
from temporalio.exceptions import ApplicationError

from scf_contracts import RunStatus, WorkflowRun, WorkflowState
from scf_workflow.red_team_gate import evaluate_red_team
from scf_workflow.store import InMemoryWorkflowStore, WorkflowStore
from scf_workflow.temporal.payloads import (
    AgentRunResultDTO,
    ApprovalInput,
    ChecksResult,
    CleanupInput,
    GuardInputs,
    GuardInputsRequest,
    NotifyInput,
    OpenPrInput,
    OpenPrResult,
    RedTeamInput,
    RedTeamResult,
    ResumeAgentInput,
    RunAgentInput,
    RunChecksInput,
    TransitionRecord,
)

logger = logging.getLogger(__name__)

#: Default verification check set (spec: default_feature ``checks``).
DEFAULT_CHECKS = ("lint", "type_check", "tests", "coverage")

#: Seconds between activity heartbeats (the agent activity's heartbeat timeout is 120 s).
AGENT_HEARTBEAT_EVERY = 30.0

#: An agent body holds its thread for the whole run, so agents get their own pool and
#: the short services (persist, guards, approvals) keep the loop's default one. Sized
#: to Temporal's default of 100 concurrent activities per worker.
_AGENT_THREADS = ThreadPoolExecutor(max_workers=100, thread_name_prefix="scf-agent")
#: Declared checks run whole test suites, so they too stay off the short services' pool.
_CHECK_THREADS = ThreadPoolExecutor(max_workers=100, thread_name_prefix="scf-checks")

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
#: The approval service is async; the injected callable may be sync or async
#: (the activity awaits an awaitable result).
ApprovalFn = Callable[[ApprovalInput], uuid.UUID | Awaitable[uuid.UUID]]
RedTeamFn = Callable[[RedTeamInput], RedTeamResult]


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
    # A stand-in reports that no PR exists rather than inventing one.
    return OpenPrResult(pr_number=None, pr_url=None)


def _default_notify(_: NotifyInput) -> bool:
    return True


def _default_cleanup(_: CleanupInput) -> bool:
    return True


def _default_approval(_: ApprovalInput) -> uuid.UUID:
    return uuid.uuid4()


def _default_red_team(inp: RedTeamInput) -> RedTeamResult:
    # No adversary model / sandbox wired: the verdict is ``not_run``, never
    # ``survived`` (#174), and the human gate is reached unchanged. A real
    # deployment injects a red_team_fn that runs a heterogeneous adversary
    # against the candidate in a sandbox. The verdict construction is the shared,
    # engine-agnostic helper (V1 mints the same).
    return evaluate_red_team(inp, red_team_fn=None)


def _refuse(activity_name: str) -> Callable[[object], NoReturn]:
    """A service stand-in that refuses: the activity is not configured."""

    def _not_configured(_: object) -> NoReturn:
        raise ApplicationError(
            f"{activity_name} is not configured on this worker; refusing rather than "
            "reporting a result it did not produce (ADR-0007)",
            type="ActivityNotConfigured",
            non_retryable=True,
        )

    return _not_configured


class WorkflowActivities:
    """All Activity implementations, bound to a store + injected services.

    The bound methods are registered on the Temporal worker. Tests inject
    scriptable fakes to drive specific paths (failing checks, awaiting input, …)
    and pass ``allow_fakes=True`` for the services they leave out.
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
        red_team_fn: RedTeamFn | None = None,
        allow_fakes: bool = False,
    ) -> None:
        def pick[I, O](
            fn: Callable[[I], O] | None, fake: Callable[[I], O], name: str
        ) -> Callable[[I], O]:
            if fn is not None:
                return fn
            return fake if allow_fakes else _refuse(name)

        self._store: WorkflowStore = store or InMemoryWorkflowStore()
        self._guard_inputs_fn = pick(
            guard_inputs_fn, _default_guard_inputs, "forge.load_guard_inputs"
        )
        self._run_agent_fn = pick(run_agent_fn, _default_run_agent, "forge.run_agent")
        self._resume_agent_fn = pick(resume_agent_fn, _default_resume_agent, "forge.resume_agent")
        self._run_checks_fn = pick(run_checks_fn, _default_run_checks, "forge.run_checks")
        self._open_pr_fn = pick(
            open_pr_fn, _default_open_pr, "forge.open_pr_with_spec_traceability"
        )
        self._notify_fn = pick(notify_fn, _default_notify, "forge.pause_and_notify")
        self._cleanup_fn = pick(cleanup_fn, _default_cleanup, "forge.cleanup_worktree")
        self._approval_fn = pick(approval_fn, _default_approval, "forge.create_approval")
        self._red_team_fn = pick(red_team_fn, _default_red_team, "forge.run_red_team_scan")

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
            self.run_red_team_scan,
        ]

    # -- projection writer (idempotent) ----------------------------------- #

    @activity.defn(name="forge.persist_transition")
    async def persist_transition(self, rec: TransitionRecord) -> int:
        return await _heartbeating(self._persist, rec, {"phase": "persist"})

    def _persist(self, rec: TransitionRecord) -> int:
        run = self._store.get(rec.workflow_run_id)
        if run.status is RunStatus.FAILED and run.context.get("start_error"):
            # The engine gave up on this start; an orphaned workflow must not revive the row.
            logger.warning(
                "run %s failed to start; not recording %s -> %s",
                rec.workflow_run_id,
                rec.from_state,
                rec.to_state,
            )
            return 0
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
                "from_state": str(rec.from_state),
                "to_state": str(rec.to_state),
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
        run.current_state = str(rec.to_state)
        run.context["last_event"] = rec.event
        if rec.temporal_run_id:
            run.context["temporal_run_id"] = rec.temporal_run_id
        _apply_status(run, str(rec.to_state), rec.run_status)
        self._store.update(run)
        return sequence

    # -- IO-bound guard resolution ---------------------------------------- #

    @activity.defn(name="forge.load_guard_inputs")
    async def load_guard_inputs(self, req: GuardInputsRequest) -> GuardInputs:
        return await _heartbeating(self._guard_inputs_fn, req, {"phase": "guard_inputs"})

    # -- agent (F06 LangGraph), heartbeating ------------------------------ #

    @activity.defn(name="forge.run_agent")
    async def run_agent(self, inp: RunAgentInput) -> AgentRunResultDTO:
        return await _heartbeating(
            self._run_agent_fn,
            inp,
            {"phase": "executing", "attempt": inp.attempt},
            executor=_AGENT_THREADS,
        )

    @activity.defn(name="forge.resume_agent")
    async def resume_agent(self, inp: ResumeAgentInput) -> AgentRunResultDTO:
        return await _heartbeating(
            self._resume_agent_fn,
            inp,
            {"phase": "resuming", "attempt": inp.attempt},
            executor=_AGENT_THREADS,
        )

    # -- verification (F08), PR (F03/F08) --------------------------------- #

    @activity.defn(name="forge.run_checks")
    async def run_checks(self, inp: RunChecksInput) -> ChecksResult:
        return await _heartbeating(
            self._run_checks_fn, inp, {"phase": "checks"}, executor=_CHECK_THREADS
        )

    @activity.defn(name="forge.open_pr_with_spec_traceability")
    async def open_pr_with_spec_traceability(self, inp: OpenPrInput) -> OpenPrResult:
        return await _heartbeating(self._open_pr_fn, inp, {"phase": "open_pr"})

    # -- notifications (F16) + approvals (F36) ---------------------------- #

    @activity.defn(name="forge.pause_and_notify")
    async def pause_and_notify(self, inp: NotifyInput) -> bool:
        return await _heartbeating(self._notify_fn, inp, {"phase": "notify"})

    @activity.defn(name="forge.create_approval")
    async def create_approval(self, inp: ApprovalInput) -> str:
        return str(await _heartbeating(self._approval_fn, inp, {"phase": "approval"}))

    # -- red-team gate (heterogeneous adversary, sandboxed) --------------- #

    @activity.defn(name="forge.run_red_team_scan")
    async def run_red_team_scan(self, inp: RedTeamInput) -> RedTeamResult:
        """Attack the candidate change; return a block/survive verdict.

        Idempotent on ``inp.idempotency_key``: the injected ``red_team_fn`` runs
        the (heterogeneous, sandboxed) adversary and records the verdict; the
        default is a parked-pass so the spine runs with no adversary/sandbox
        wired. Persisting the ``RedTeamRecord`` + emitting the ``redteam.survived``
        audit event is the recorder's job inside ``red_team_fn`` (real wiring);
        the pure verdict returned here drives the deterministic routing.
        """
        details = {"phase": "red_team", "idempotency_key": inp.idempotency_key}
        return await _heartbeating(self._red_team_fn, inp, details)

    # -- compensation ----------------------------------------------------- #

    @activity.defn(name="forge.cleanup_worktree")
    async def cleanup_worktree(self, inp: CleanupInput) -> bool:
        return await asyncio.to_thread(self._cleanup_fn, inp)  # git + DB work blocks


async def _heartbeating[I, O](
    fn: Callable[[I], O | Awaitable[O]],
    inp: I,
    details: Mapping[str, object],
    *,
    executor: Executor | None = None,
) -> O:
    """Run a service off the event loop, heartbeating until it returns.

    Services block (git, the database, a model loop); on the loop they would stall
    the heartbeats, so a long one outlives its heartbeat timeout. An activity cancel
    (delivered through the heartbeat) cancels this coroutine instead of waiting.
    """

    def call() -> O:
        result = fn(inp)
        if inspect.isawaitable(result):  # an async service gets its own loop in this thread
            return asyncio.run(_awaited(result))
        return result

    # shortcut: a cancelled service thread runs to completion, until services take a cancel token
    loop = asyncio.get_running_loop()
    # ``executor=None`` is the loop's default pool, as ``asyncio.to_thread`` uses.
    work = loop.run_in_executor(executor, contextvars.copy_context().run, call)
    while True:
        if activity.in_activity():  # tests call some activities directly
            activity.heartbeat(details)
        done, _ = await asyncio.wait({work}, timeout=AGENT_HEARTBEAT_EVERY)
        if done:
            return work.result()


async def _awaited[O](result: Awaitable[O]) -> O:
    return await result


def _apply_status(run: WorkflowRun, new_state: str, run_status: str | None = None) -> None:
    if run_status is not None:
        # Station runs (spec K §4.2): the line's terminals set the status.
        run.status = RunStatus(run_status)
        if run.status in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED):
            run.completed_at = _now()
    elif new_state in _SUCCESS_STATES:
        run.status = RunStatus.SUCCEEDED
        run.completed_at = _now()
    elif new_state in _STATUS_BY_STATE:
        run.status = _STATUS_BY_STATE[new_state]
        if run.status in (RunStatus.FAILED, RunStatus.CANCELLED):
            run.completed_at = _now()
    else:
        run.status = RunStatus.RUNNING


__all__ = [
    "AGENT_HEARTBEAT_EVERY",
    "DEFAULT_CHECKS",
    "ApprovalFn",
    "WorkflowActivities",
]
