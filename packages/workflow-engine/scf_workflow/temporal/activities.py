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
delivery never double-writes a transition. ``persist_station`` writes a line run's
``workflow_station_visit`` rows the same way, keyed by the visit id (spec K §4.3).
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, NoReturn

from temporalio import activity
from temporalio.exceptions import ApplicationError

from scf_contracts import RunStatus, WorkflowRun, WorkflowState
from scf_contracts.lines import SERVICE_CATALOG
from scf_workflow.red_team_gate import evaluate_red_team
from scf_workflow.store import (
    STATION_KINDS,
    STATION_STATUS_RANK,
    InMemoryWorkflowStore,
    StationVisitStore,
    WorkflowStore,
)
from scf_workflow.temporal.converter import REDACTED, redact_secrets
from scf_workflow.temporal.ids import station_visit_id
from scf_workflow.temporal.payloads import (
    AgentRunResultDTO,
    ApprovalInput,
    ChecksResult,
    CleanupInput,
    DraftScenariosInput,
    DraftScenariosResult,
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
    ServiceStationInput,
    ServiceStationResult,
    StationGateInput,
    StationGateResult,
    StationVisitRecord,
    TransitionRecord,
)

logger = logging.getLogger(__name__)

#: Default verification check set (spec: default_feature ``checks``).
DEFAULT_CHECKS = ("lint", "type_check", "tests", "coverage")

#: A station visit's stored outputs are at most this many bytes of JSON (spec K §4.3).
STATION_OUTPUTS_CAP = 16 * 1024
#: ``workflow_station_visit`` column widths; Postgres refuses a longer value, or a NUL.
_VISIT_WIDTHS = {"state": 64, "outcome": 48, "error_code": 64, "service_name": 64}

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
DraftScenariosFn = Callable[[DraftScenariosInput], DraftScenariosResult]
#: A service station's body, bound by catalog name (spec K §4.1).
ServiceFn = Callable[[ServiceStationInput], ServiceStationResult]
#: Opens or reads a human station's gate (spec K §4.2); sync or async.
StationGateFn = Callable[[StationGateInput], StationGateResult | Awaitable[StationGateResult]]


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


def _default_draft_scenarios(_: DraftScenariosInput) -> DraftScenariosResult:
    # No verifier wired: nothing is drafted, and the result says why (#258).
    return DraftScenariosResult(reason="verifier_not_configured")


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
        draft_scenarios_fn: DraftScenariosFn | None = None,
        service_bodies: Mapping[str, ServiceFn] | None = None,
        open_station_gate_fn: StationGateFn | None = None,
        read_station_gate_fn: StationGateFn | None = None,
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
        self._draft_scenarios_fn = pick(
            draft_scenarios_fn, _default_draft_scenarios, "forge.draft_held_out_scenarios"
        )
        # No stand-in, even with fakes: an unbound service refuses (``not_configured``).
        self._service_bodies = dict(service_bodies or {})
        # Nor for a human gate: a stand-in decision would be one nobody made.
        self._open_station_gate_fn = open_station_gate_fn or _refuse("forge.open_station_gate")
        self._read_station_gate_fn = read_station_gate_fn or _refuse("forge.read_station_gate")

    @property
    def store(self) -> WorkflowStore:
        return self._store

    def register(self) -> list[Callable[..., Awaitable[object]]]:
        """The bound activity methods to register on a worker."""
        return [
            self.persist_transition,
            self.persist_station,
            self.load_guard_inputs,
            self.run_agent,
            self.resume_agent,
            self.run_checks,
            self.open_pr_with_spec_traceability,
            self.pause_and_notify,
            self.cleanup_worktree,
            self.create_approval,
            self.run_red_team_scan,
            self.draft_held_out_scenarios,
            self.run_service_station,
            self.open_station_gate,
            self.read_station_gate,
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

    @activity.defn(name="forge.persist_station")
    async def persist_station(self, rec: StationVisitRecord) -> str:
        """Record one station visit and mirror it into the run's context (spec K §4.3, §4.11).

        An idempotent upsert by the visit id whose status only moves forward, so a
        late retry of ``running`` never undoes ``succeeded``. Outputs and the error
        message are redacted, and outputs are capped at 16 KiB. The run's state and
        status stay ``persist_transition``'s. Returns the visit id.
        """
        return await _heartbeating(self._persist_station, rec, {"phase": "persist_station"})

    def _persist_station(self, rec: StationVisitRecord) -> str:
        store = self._store
        if not isinstance(store, StationVisitStore):
            _refuse("forge.persist_station")(rec)
        unfit = [
            name
            for name, width in _VISIT_WIDTHS.items()
            if (value := getattr(rec, name)) and (len(value) > width or "\x00" in value)
        ]
        if rec.status not in STATION_STATUS_RANK or rec.kind not in STATION_KINDS or unfit:
            # Every retry would fail the same way, so fail now.
            raise ApplicationError(
                f"station visit not recordable: kind {rec.kind!r}, status {rec.status!r}, "
                f"unfit fields {unfit}",
                type="InvalidStationVisit",
                non_retryable=True,
            )
        error = _text(redact_secrets(rec.error_message)) if rec.error_message else None
        store.save_station_visit(
            replace(rec, outputs=_station_outputs(rec.outputs), error_message=error)
        )
        run = store.get(rec.workflow_run_id)
        _mirror_visits(run, store.list_station_visits(rec.workflow_run_id))
        store.update(run)
        return str(station_visit_id(rec.workflow_run_id, rec.state, rec.visit))

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

    @activity.defn(name="forge.draft_held_out_scenarios")
    async def draft_held_out_scenarios(self, inp: DraftScenariosInput) -> DraftScenariosResult:
        """A verifier on another model family drafts held-out scenarios (#258).

        Runs an agent, so it heartbeats on the agents' pool.
        """
        details = {"phase": "draft_held_out_scenarios", "workflow_run_id": str(inp.workflow_run_id)}
        return await _heartbeating(self._draft_scenarios_fn, inp, details, executor=_AGENT_THREADS)

    @activity.defn(name="forge.run_red_team_scan")
    async def run_red_team_scan(self, inp: RedTeamInput) -> RedTeamResult:
        """Attack the candidate change; return a block/survive verdict.

        Idempotent on ``inp.idempotency_key``: the injected ``red_team_fn`` runs
        the (heterogeneous, sandboxed) adversary and records the verdict; the
        default is ``not_run``, never a pass, so the spine runs with no
        adversary/sandbox wired (#174). Persisting the ``RedTeamRecord`` +
        emitting the ``redteam.survived`` audit event is the recorder's job inside
        ``red_team_fn`` (real wiring);
        the pure verdict returned here drives the deterministic routing.
        """
        details = {"phase": "red_team", "idempotency_key": inp.idempotency_key}
        return await _heartbeating(self._red_team_fn, inp, details)

    # -- line service stations (spec K §4.2) ------------------------------ #

    @activity.defn(name="forge.run_service_station")
    async def run_service_station(self, inp: ServiceStationInput) -> ServiceStationResult:
        """Run the body bound to ``inp.name``; an unbound name refuses (``not_configured``).

        Only the outputs the catalog declares, of their declared type, are kept.
        """
        body = self._service_bodies.get(inp.name) or _refuse(inp.name)
        details = {"phase": "service_station", "service": inp.name}
        result = await _heartbeating(body, inp, details, executor=_CHECK_THREADS)
        entry = SERVICE_CATALOG.get(inp.name)
        declared = entry.outputs if entry else {}
        result.outputs = {
            k: v
            for k, v in result.outputs.items()
            if k in declared and isinstance(v, declared[k][0])
        }
        return result

    # -- line human stations (spec K §4.2) -------------------------------- #

    @activity.defn(name="forge.open_station_gate")
    async def open_station_gate(self, inp: StationGateInput) -> StationGateResult:
        """Open the visit's approval request, or return the one it has in any status."""
        return await _heartbeating(self._open_station_gate_fn, inp, {"phase": "open_gate"})

    @activity.defn(name="forge.read_station_gate")
    async def read_station_gate(self, inp: StationGateInput) -> StationGateResult:
        """Read the visit's request and decisions; past the deadline, expire it if pending."""
        return await _heartbeating(self._read_station_gate_fn, inp, {"phase": "read_gate"})

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


def _text(value: str) -> str:
    """``value`` as Postgres text and JSONB take it: NUL and lone surrogates escaped."""
    return value.encode("utf-8", "backslashreplace").decode().replace("\x00", "\\x00")


def _redacted(value: Any) -> Any:
    """``value`` with secrets masked, as the history codec masks them in its JSON text."""
    if isinstance(value, str):
        return _text(redact_secrets(value))
    if isinstance(value, list):
        return [_redacted(item) for item in value]
    if isinstance(value, dict):
        # A key the codec's ``key=value`` pattern names (``token``, ``db_password``, ...).
        return {
            _text(str(k)): REDACTED if redact_secrets(f"{k}=x") != f"{k}=x" else _redacted(v)
            for k, v in value.items()
        }
    return value


def _station_outputs(outputs: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = _redacted(outputs)
    # The UTF-8 bytes stored, not the ASCII-escaped JSON six times the size.
    text = json.dumps(redacted, default=str, ensure_ascii=False)
    size = len(text.encode("utf-8", "surrogatepass"))
    return redacted if size <= STATION_OUTPUTS_CAP else {"truncated": True, "bytes": size}


def _mirror_visits(run: WorkflowRun, visits: list[StationVisitRecord]) -> None:
    """Rebuild the run's ``outputs``, ``spend`` and ``parked`` from all its visits.

    Rebuilt rather than added to, so a retried or late report never counts twice.
    """
    latest: dict[str, StationVisitRecord] = {}
    for v in visits:
        if v.state not in latest or v.visit > latest[v.state].visit:
            latest[v.state] = v
    run.context["outputs"] = {state: v.outputs for state, v in latest.items()}
    run.context["spend"] = {
        "tokens": sum(v.tokens_used for v in visits),
        "usd": round(sum(v.cost_usd or 0.0 for v in visits if v.priced), 4),
        "unpriced_tokens": sum(v.tokens_used for v in visits if not v.priced),
    }
    parked = [v for v in latest.values() if v.status == "parked"]
    if parked:
        run.context["parked"] = {
            "state": parked[-1].state,
            "visit": parked[-1].visit,
            "reason": parked[-1].outcome,
        }
    else:
        run.context.pop("parked", None)


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
    "STATION_OUTPUTS_CAP",
    "ApprovalFn",
    "ServiceFn",
    "StationGateFn",
    "WorkflowActivities",
]
