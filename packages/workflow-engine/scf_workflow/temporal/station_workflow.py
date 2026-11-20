"""The durable line interpreter: ``forge.StationWorkflow`` (spec K §4.2, ADR-0020).

A line run enters a state, runs its station (agent, service or human), and routes the
station's outcome with the pure :class:`TransitionEvaluator` over string states.
Every effect is an activity: ``forge.persist_station`` records each visit,
``forge.persist_transition`` the run's state and status. Visits are bounded by
``max_visits`` and agent stations by the line's token budget. An outcome with no
edge parks the run (``escalated``) or fails it. A parked run waits for ``resume``
(an Update, from a user); ``cancel_run`` (a Signal) cancels the in-flight activity,
cleans up and ends the run as ``cancelled``.

A human station opens its visit's approval request and waits; its outcome and the
transition's actor are only what ``forge.read_station_gate`` reads from Postgres. The
``station_gate_changed`` Signal only wakes the wait, which also wakes itself every 15
minutes and at the gate's deadline (ADR-0020).

The params carry the revision's text, so replay never reads the database.
"""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import suppress
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, FailureError
from temporalio.exceptions import TimeoutError as ActivityTimeoutError

with workflow.unsafe.imports_passed_through():
    import yaml

    from scf_contracts.lines import (
        FAILURE_OUTCOMES,
        PLACEHOLDER_RE,
        SERVICE_CATALOG,
        SUCCEEDED,
        UNROUTABLE_OUTCOMES,
        AgentStation,
        HumanStation,
        LineDefinition,
        ServiceStation,
        parse_line,
        station_outcomes,
    )
    from scf_workflow.exceptions import GuardFailedError, InvalidTransitionError
    from scf_workflow.temporal.determinism import PureGuardContext, TransitionEvaluator
    from scf_workflow.temporal.ids import station_visit_id, transition_idempotency_key
    from scf_workflow.temporal.payloads import (
        EVENT_CANCEL,
        EVENT_RESUME,
        AgentRunResultDTO,
        CleanupInput,
        RunAgentInput,
        ServiceStationInput,
        ServiceStationResult,
        StationGateInput,
        StationGateResult,
        StationParams,
        StationVisitRecord,
        TransitionRecord,
        WorkflowEventPayload,
    )

#: An unrouted outcome in this set parks the run; any other unrouted outcome fails it.
#: A ``pr`` gate approved with no attestation recorded for it: never routable, it parks.
ATTESTATION_MISSING = "attestation_missing"
PARK_OUTCOMES = frozenset(
    {"awaiting_input", "low_confidence", "expired", "visit_limit", "no_checks", ATTESTATION_MISSING}
)
VISIT_LIMIT = "visit_limit"
BUDGET_EXHAUSTED = "budget_exhausted"
#: Control events (the line has no edge for them).
EVENT_DSL_INVALID = "dsl_invalid"
EVENT_ACTIVITY_FAILED = "activity_failed"

_PERSIST_TIMEOUT = timedelta(seconds=30)
_CLEANUP_TIMEOUT = timedelta(seconds=60)
_HEARTBEAT = timedelta(seconds=120)
_PERSIST_RETRY = RetryPolicy(maximum_attempts=0)  # 0 == unlimited
_ONCE = RetryPolicy(maximum_attempts=1)
_THRICE = RetryPolicy(maximum_attempts=3)
_GATE_TIMEOUT = timedelta(seconds=60)
#: A human station re-reads its gate at least this often, Signal or not.
_GATE_WAKE = timedelta(minutes=15)
#: Runs recorded before human stations ran failed at them; they replay that way.
HUMAN_STATIONS_PATCH = "human-stations"


class _Cancelled(Exception):
    """Internal: unwind to the cancel path."""


def load_line(dsl_yaml: str) -> LineDefinition:
    """Parse a revision's text; raises on YAML, an unknown ``dsl_version`` or a bad shape."""
    raw = yaml.safe_load(dsl_yaml)
    if not isinstance(raw, dict):
        raise ValueError("a line is a mapping")
    return parse_line(raw)


def _label(path: str, value: object) -> str:
    """A value substituted into a prompt, with its source (ADR-0014).

    The markers carry a hash of the value, so the value cannot contain its own end.
    """
    text = str(value)
    tag = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:12]
    return f"[source: {path} {tag}] {text} [end of {path} {tag}]"


@workflow.defn(name="forge.StationWorkflow")
class StationWorkflow:
    """Durable interpreter of one line run (agent and service stations)."""

    def __init__(self) -> None:
        self._params: StationParams | None = None
        self._state = ""
        self._seq = 0
        self._visits: dict[str, int] = {}
        #: Raised past ``max_visits`` by a resume after ``visit_limit``.
        self._limits: dict[str, int] = {}
        self._outputs: dict[str, dict[str, Any]] = {}
        self._flags: dict[str, bool] = {}
        self._tokens = 0
        #: A person's input for the next visit of a state parked ``awaiting_input``.
        self._input: dict[str, str] = {}
        self._running: StationVisitRecord | None = None
        self._awaiting: list[str] = []
        self._resume: WorkflowEventPayload | None = None
        self._cancel_requested = False
        self._cancel_reason = "cancelled"
        self._cancel_actor = "system"
        self._gate_hint = False

    @workflow.run
    async def run(self, params: StationParams) -> str:
        self._params = params
        try:
            line = load_line(params.dsl_yaml)
        except Exception as exc:  # any parse failure: fail the run, never retry
            reason = f"{type(exc).__name__}: {exc}"[:500]
            await self._transition("failed", EVENT_DSL_INVALID, "failed", {"reason": reason})
            raise ApplicationError(reason, type="LineInvalid", non_retryable=True) from exc
        terminals = line.all_terminals
        evaluator = TransitionEvaluator(line, state_cast=str)
        self._state = line.initial
        try:
            while self._state not in terminals:
                self._check_cancel()
                await self._visit(line, evaluator, terminals)
        except _Cancelled:
            return await self._do_cancel()
        except ActivityError as exc:
            # The run records why it ended before the execution fails (as #278).
            cause = exc.cause if isinstance(exc.cause, FailureError) else exc
            reason = f"{type(cause).__name__}: {cause.message}"[:500]
            await self._transition("failed", EVENT_ACTIVITY_FAILED, "failed", {"reason": reason})
            raise
        return self._state

    # ------------------------------------------------------------------ #
    # One visit                                                           #
    # ------------------------------------------------------------------ #
    async def _visit(
        self, line: LineDefinition, evaluator: TransitionEvaluator, terminals: dict[str, str]
    ) -> None:
        assert self._params is not None
        state = self._state
        station = line.stations[state]
        visit = self._visits.get(state, 0) + 1
        self._visits[state] = visit
        rec = StationVisitRecord(
            workflow_run_id=self._params.workflow_run_id,
            workspace_id=self._params.workspace_id,
            definition_revision_id=self._params.definition_revision_id,
            state=state,
            visit=visit,
            kind=station.kind,
            status="running",
            service_name=station.service if isinstance(station, ServiceStation) else None,
        )
        actor = "system"
        if visit > self._limits.get(state, station.max_visits):
            rec.outcome = VISIT_LIMIT
        elif isinstance(station, AgentStation) and self._budget_left(line) <= 0:
            rec.outcome = rec.error_code = BUDGET_EXHAUSTED
        else:
            await self._persist_station(rec)
            self._running = rec
            if isinstance(station, AgentStation):
                await self._run_agent(line, station, rec)
            elif isinstance(station, ServiceStation):
                await self._run_service(station, rec)
            elif workflow.patched(HUMAN_STATIONS_PATCH):
                actor = await self._run_human(station, rec)
            else:
                rec.outcome, rec.error_code = "failed", "station_kind_unsupported"
            self._running = None
        outcome = rec.outcome or "failed"

        to_state, guards, park = state, {}, False
        try:
            decision = evaluator.resolve(
                state, outcome, pure_guard_ctx=PureGuardContext(station_flags=dict(self._flags))
            )
            to_state, guards = str(decision.to_state), decision.guard_results
        except InvalidTransitionError:
            park = outcome in PARK_OUTCOMES
            to_state = state if park else "failed"
        except GuardFailedError:
            to_state = "failed"  # a guard left no edge enabled: fail closed

        failure = FAILURE_OUTCOMES | UNROUTABLE_OUTCOMES
        if isinstance(station, ServiceStation) and station.service in SERVICE_CATALOG:
            failure |= SERVICE_CATALOG[station.service].failure_outcomes
        rec.status = "parked" if park else ("failed" if outcome in failure else "succeeded")
        await self._persist_station(rec)

        payload: dict[str, Any] = {"visit": visit}
        if park:
            payload["parked"] = outcome
            await self._transition(state, outcome, "escalated", payload)
            await self._park(state, outcome)
            return
        await self._transition(
            to_state,
            outcome,
            terminals.get(to_state, "running"),
            payload,
            actor=actor,
            guards=guards,
        )

    def _budget_left(self, line: LineDefinition) -> int:
        # validate_line requires a line budget whenever an agent station exists.
        return (line.budget.max_tokens if line.budget else 0) - self._tokens

    def _refs(self) -> dict[str, Any]:
        """Every reference path that resolves now (spec K §4.1)."""
        assert self._params is not None
        refs: dict[str, Any] = {f"task.{k}": v for k, v in self._params.task.items()}
        refs["task.id"] = str(self._params.task_id)
        refs["run.id"] = str(self._params.workflow_run_id)
        if self._params.trigger.get("external_ref") is not None:
            refs["trigger.external_ref"] = self._params.trigger["external_ref"]
        for state, outputs in self._outputs.items():
            refs.update({f"outputs.{state}.{k}": v for k, v in outputs.items()})
        return refs

    async def _run_agent(
        self, line: LineDefinition, station: AgentStation, rec: StationVisitRecord
    ) -> None:
        assert self._params is not None
        left = self._budget_left(line)
        cap = min(station.budget.max_tokens, left) if station.budget else left
        refs = self._refs()
        objective = PLACEHOLDER_RE.sub(
            lambda m: _label(m[1], refs[m[1]]) if m[1] in refs else "", station.objective
        )
        if note := self._input.pop(rec.state, None):
            objective += "\n\n" + _label("user_input", note)
        key = f"station:{self._params.workflow_run_id}:{rec.state}:{rec.visit}"
        inp = RunAgentInput(
            workflow_run_id=self._params.workflow_run_id,
            task_id=self._params.task_id,
            workspace_id=self._params.workspace_id,
            attempt=rec.visit,
            idempotency_key=key,
            station_visit_id=station_visit_id(self._params.workflow_run_id, rec.state, rec.visit),
            role=station.role.value,
            skill_profile=station.skill_profile,
            objective=objective,
            context_template=station.context_template,
            model=station.model,
            max_tokens=cap,
        )
        try:
            result: AgentRunResultDTO = await self._cancellable(
                workflow.start_activity(
                    "forge.run_agent",
                    arg=inp,
                    start_to_close_timeout=timedelta(seconds=station.timeout_seconds),
                    heartbeat_timeout=_HEARTBEAT,
                    retry_policy=_ONCE,
                    result_type=AgentRunResultDTO,
                )
            )
        except ActivityError as exc:
            _record_error(rec, exc)
            self._tokens += cap  # what it spent is unknown: charge the whole cap
            return
        self._tokens += result.tokens_used
        rec.agent_run_id = result.agent_run_id
        rec.tokens_used, rec.cost_usd, rec.priced = (
            result.tokens_used,
            result.cost_usd,
            result.priced,
        )
        outputs = {"branch": result.branch_name, "head_sha": result.head_commit_sha}
        rec.outputs = {k: v for k, v in outputs.items() if v}
        self._outputs[rec.state] = rec.outputs
        rec.outcome = _agent_outcome(station, result)

    async def _run_service(self, station: ServiceStation, rec: StationVisitRecord) -> None:
        assert self._params is not None
        refs = self._refs()
        unresolved = sorted(name for name, path in station.inputs.items() if path not in refs)
        if unresolved:  # before any body runs
            rec.outcome, rec.error_code = "failed", "input_unresolved"
            rec.error_message = f"unresolved inputs: {', '.join(unresolved)}"
            return
        entry = SERVICE_CATALOG[station.service]
        attempts = entry.max_attempts if entry.idempotent else 1
        if station.retry is not None:
            attempts = min(station.retry.max_attempts, attempts)
        timeout = min(
            station.timeout_seconds or entry.max_timeout_seconds, entry.max_timeout_seconds
        )
        inp = ServiceStationInput(
            workflow_run_id=self._params.workflow_run_id,
            workspace_id=self._params.workspace_id,
            name=station.service,
            idempotency_key=f"station:{self._params.workflow_run_id}:{rec.state}:{rec.visit}",
            params=dict(station.params),
            inputs={name: refs[path] for name, path in station.inputs.items()},
        )
        try:
            result: ServiceStationResult = await self._cancellable(
                workflow.start_activity(
                    "forge.run_service_station",
                    arg=inp,
                    start_to_close_timeout=timedelta(seconds=timeout),
                    heartbeat_timeout=_HEARTBEAT,
                    retry_policy=RetryPolicy(maximum_attempts=attempts),
                    result_type=ServiceStationResult,
                )
            )
        except ActivityError as exc:
            _record_error(rec, exc)
            return
        rec.outputs, rec.error_code, rec.error_message = (
            result.outputs,
            result.error_code,
            result.error_message,
        )
        rec.outcome = result.outcome
        if result.outcome not in station_outcomes(station):
            rec.outcome, rec.error_code = "failed", "unknown_outcome"
        self._outputs[rec.state] = result.outputs
        prefix = f"{rec.state}."
        self._flags = {k: v for k, v in self._flags.items() if not k.startswith(prefix)}
        self._flags.update(
            {prefix + k: v for k, v in result.outputs.items() if isinstance(v, bool)}
        )

    async def _run_human(self, station: HumanStation, rec: StationVisitRecord) -> str:
        """Open the visit's gate and wait for it; returns the transition's actor.

        Every wake reads the gate; past the deadline the read expires it if it is still
        pending, so the loop always ends in a decision or ``expired``.
        """
        assert self._params is not None
        deadline = workflow.now() + timedelta(seconds=station.sla_seconds)
        refs = self._refs()
        inp = StationGateInput(
            workflow_run_id=self._params.workflow_run_id,
            workspace_id=self._params.workspace_id,
            state=rec.state,
            visit=rec.visit,
            gate=station.gate.value,
            expires_at=deadline,
            required_approvals=station.required_approvals,
            summary=PLACEHOLDER_RE.sub(lambda m: str(refs.get(m[1], "")), station.summary or ""),
        )
        try:
            # Cancellable: a pr gate reads the repository's policy, which can stall.
            gate: StationGateResult = await self._cancellable(
                workflow.start_activity(
                    "forge.open_station_gate",
                    arg=inp,
                    start_to_close_timeout=_GATE_TIMEOUT,
                    heartbeat_timeout=_HEARTBEAT,
                    retry_policy=_PERSIST_RETRY,
                    result_type=StationGateResult,
                )
            )
            rec.approval_request_id, rec.status = gate.approval_request_id, "waiting"
            await self._persist_station(rec)
            while True:
                left = (deadline - workflow.now()).total_seconds()
                if left > 0:
                    with suppress(TimeoutError):
                        await workflow.wait_condition(
                            lambda: self._gate_hint or self._cancel_requested,
                            timeout=min(_GATE_WAKE.total_seconds(), left),
                        )
                self._check_cancel()
                self._gate_hint = False
                inp.expire = workflow.now() >= deadline
                gate = await workflow.execute_activity(
                    "forge.read_station_gate",
                    arg=inp,
                    start_to_close_timeout=_GATE_TIMEOUT,
                    retry_policy=_PERSIST_RETRY,
                    result_type=StationGateResult,
                )
                if gate.status != "pending":
                    break
        except ActivityError as exc:
            _record_error(rec, exc)
            return "system"
        rec.outcome = gate.status
        return gate.actor or "system"

    async def _cancellable(self, handle: Any) -> Any:
        """Await an activity; a cancel request cancels it (through its heartbeat)."""
        await workflow.wait_condition(lambda: handle.done() or self._cancel_requested)
        if not handle.done():
            handle.cancel()
            with suppress(ActivityError, asyncio.CancelledError):
                await handle
            raise _Cancelled()
        return handle.result()

    # ------------------------------------------------------------------ #
    # Park / resume / cancel                                              #
    # ------------------------------------------------------------------ #
    async def _park(self, state: str, reason: str) -> None:
        self._awaiting = [EVENT_RESUME]
        await workflow.wait_condition(lambda: self._cancel_requested or self._resume is not None)
        self._check_cancel()
        ev = self._resume
        assert ev is not None
        payload: dict[str, Any] = {}
        if reason == VISIT_LIMIT:
            # One more visit for the station (spec K §4.11).
            self._limits[state] = self._visits[state] + 1
            payload["visits_allowed"] = self._limits[state]
        note = ev.payload.get("input")
        if reason == "awaiting_input" and isinstance(note, str) and note:
            self._input[state] = note
        await self._transition(state, EVENT_RESUME, "running", payload, actor=ev.actor)
        self._resume = None

    async def _do_cancel(self) -> str:
        assert self._params is not None
        if self._running is not None:
            self._running.status = "cancelled"
            await self._persist_station(self._running)
        await workflow.execute_activity(
            "forge.cleanup_worktree",
            arg=CleanupInput(
                workflow_run_id=self._params.workflow_run_id,
                workspace_id=self._params.workspace_id,
                reason=self._cancel_reason,
                idempotency_key=f"{self._params.workflow_run_id}:cleanup",
                actor=self._cancel_actor,
            ),
            start_to_close_timeout=_CLEANUP_TIMEOUT,
            retry_policy=_THRICE,
        )
        await self._transition(
            "cancelled",
            EVENT_CANCEL,
            "cancelled",
            {"reason": self._cancel_reason},
            actor=self._cancel_actor,
            effects=["cleanup_worktree"],
        )
        return self._state

    def _check_cancel(self) -> None:
        if self._cancel_requested:
            raise _Cancelled()

    @workflow.update
    async def submit_event(self, event: WorkflowEventPayload) -> str:
        """Resume a parked run; returns its state once the resume is recorded."""
        self._awaiting = []
        self._resume = event
        await workflow.wait_condition(lambda: self._resume is None or self._cancel_requested)
        return self._state

    @submit_event.validator
    def _validate_event(self, event: WorkflowEventPayload) -> None:
        if event.type not in self._awaiting:
            raise ApplicationError(
                f"no enabled transition from {self._state!r} on event {event.type!r}; "
                f"allowed={self._awaiting}",
                type="InvalidTransitionError",
            )
        if not event.actor.startswith("user:"):
            # Agents, automations and system principals cannot un-park a run.
            raise ApplicationError(
                "only a user may resume a line run", type="InvalidTransitionError"
            )

    @workflow.signal
    async def cancel_run(self, reason: str = "cancelled", actor: str | None = None) -> None:
        self._cancel_requested = True
        self._cancel_reason = reason or "cancelled"
        if actor:
            self._cancel_actor = actor

    @workflow.signal
    def station_gate_changed(self, approval_request_id: str) -> None:
        """Wake a human station to read its gate now. The id is never trusted or used."""
        self._gate_hint = True

    @workflow.query
    def current_state(self) -> str:
        return self._state

    @workflow.query
    def awaiting(self) -> list[str]:
        return list(self._awaiting)

    @workflow.query
    def transition_count(self) -> int:
        return self._seq

    # ------------------------------------------------------------------ #
    # Activity wrappers                                                   #
    # ------------------------------------------------------------------ #
    async def _persist_station(self, rec: StationVisitRecord) -> None:
        await workflow.execute_activity(
            "forge.persist_station",
            arg=rec,
            start_to_close_timeout=_PERSIST_TIMEOUT,
            retry_policy=_PERSIST_RETRY,
            result_type=str,
        )

    async def _transition(
        self,
        to_state: str,
        event: str,
        run_status: str,
        payload: dict[str, Any],
        *,
        actor: str = "system",
        guards: dict[str, bool] | None = None,
        effects: list[str] | None = None,
    ) -> None:
        assert self._params is not None
        rec = TransitionRecord(
            workflow_run_id=self._params.workflow_run_id,
            workspace_id=self._params.workspace_id,
            from_state=self._state,
            to_state=to_state,
            event=event,
            idempotency_key=transition_idempotency_key(self._params.workflow_run_id, self._seq + 1),
            guard_results=guards or {},
            effects_dispatched=effects or [],
            actor=actor,
            payload=payload,
            temporal_run_id=workflow.info().run_id,
            run_status=run_status,
        )
        await workflow.execute_activity(
            "forge.persist_transition",
            arg=rec,
            start_to_close_timeout=_PERSIST_TIMEOUT,
            retry_policy=_PERSIST_RETRY,
            result_type=int,
        )
        self._seq += 1
        self._state = to_state


def _agent_outcome(station: AgentStation, result: AgentRunResultDTO) -> str:
    if result.status == "awaiting_input":
        return "awaiting_input"
    if result.status != "succeeded":
        return "failed"
    if result.confidence < station.confidence_threshold:
        return "low_confidence"
    if station.decisions:
        # A missing or unknown decision is low confidence, which parks (spec K §4.11).
        return result.decision if result.decision in station.decisions else "low_confidence"
    return SUCCEEDED


def _record_error(rec: StationVisitRecord, exc: ActivityError) -> None:
    cause = exc.cause
    if isinstance(cause, ActivityTimeoutError):
        rec.outcome = "timed_out"
    elif isinstance(cause, ApplicationError) and cause.type == "ActivityNotConfigured":
        rec.outcome = "not_configured"
    else:
        rec.outcome = "failed"
    rec.error_code = rec.outcome
    rec.error_message = (cause.message if isinstance(cause, FailureError) else str(exc))[:500]


__all__ = ["ATTESTATION_MISSING", "PARK_OUTCOMES", "StationWorkflow", "load_line"]
