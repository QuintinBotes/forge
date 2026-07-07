"""The durable top-level orchestrator: ``forge.FeatureWorkflow`` (F25).

This is the V2 spine. It drives the exact ``default_feature`` lifecycle the V1
Postgres FSM drives (``created → … → closed`` plus the error states), but as a
durable Temporal Workflow that survives worker/API restarts mid-run. Routing is
the **pure** :class:`TransitionEvaluator` (no LLM, no IO in the spine); every
*effect* is an idempotent Activity; human/agent gates are synchronous **Workflow
Updates** (``submit_event``); cancel is a **Signal**; reads are **Queries**;
retry/backoff is a durable ``workflow.sleep``-style timer.

Determinism is the central constraint: the body uses only ``workflow.*`` time +
Activities + the pure evaluator. DB-reading guards are resolved by the
``load_guard_inputs`` Activity *outside* the deterministic core and then evaluated
deterministically here. ``resume`` / ``cancel`` are durable-engine controls (the
foundation ``default_feature`` DSL has no edges for them), recorded as transitions
with synthetic events — a documented F25 extension over the V1 FSM.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy as ActivityRetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from forge_contracts import WorkflowState
    from forge_workflow.default_workflow import default_feature_definition
    from forge_workflow.exceptions import (
        GuardFailedError,
        InvalidTransitionError,
        PreconditionError,
        WorkflowError,
    )
    from forge_workflow.temporal.determinism import PureGuardContext, TransitionEvaluator
    from forge_workflow.temporal.ids import transition_idempotency_key
    from forge_workflow.temporal.payloads import (
        EVENT_CANCEL,
        EVENT_PLAN_APPROVED,
        EVENT_RESUME,
        EVENT_REVIEW_APPROVED,
        EVENT_SPEC_APPROVED,
        EVENT_SPEC_CHANGES,
        AgentRunResultDTO,
        ApprovalInput,
        ChecksResult,
        CleanupInput,
        GuardInputs,
        GuardInputsRequest,
        NotifyInput,
        OpenPrInput,
        RunAgentInput,
        RunChecksInput,
        TransitionRecord,
        WorkflowEventPayload,
        WorkflowParams,
        WorkflowResult,
    )

# Definitions are parsed once at import (pure: a constant YAML string).
_DEFINITIONS = {default_feature_definition().name: default_feature_definition()}

# Activity-call timeouts (deterministic constants).
_PERSIST_TIMEOUT = timedelta(seconds=30)
_GUARD_TIMEOUT = timedelta(seconds=30)
_CHECKS_TIMEOUT = timedelta(seconds=120)
_PR_TIMEOUT = timedelta(seconds=120)
_NOTIFY_TIMEOUT = timedelta(seconds=30)
_APPROVAL_TIMEOUT = timedelta(seconds=30)
_CLEANUP_TIMEOUT = timedelta(seconds=60)
_DEFAULT_AGENT_TIMEOUT = timedelta(seconds=7200)
_AGENT_HEARTBEAT = timedelta(seconds=120)

_PERSIST_RETRY = ActivityRetryPolicy(maximum_attempts=0)  # 0 == unlimited
_THRICE = ActivityRetryPolicy(maximum_attempts=3)
_FIVE = ActivityRetryPolicy(maximum_attempts=5)
_ONCE = ActivityRetryPolicy(maximum_attempts=1)


class _Cancelled(Exception):
    """Internal: unwind the run() body to the cancel/compensation path."""


def _backoff_seconds(initial: int, backoff: str, attempt: int) -> int:
    if backoff == "exponential":
        return initial * (2**attempt)
    if backoff == "linear":
        return initial * (attempt + 1)
    return initial


@workflow.defn(name="forge.FeatureWorkflow")
class FeatureWorkflow:
    """Durable ``default_feature`` orchestrator (implements the F25 spine)."""

    def __init__(self) -> None:
        self._state: WorkflowState = WorkflowState.CREATED
        self._seq = 0
        self._retry_count = 0
        self._awaiting: list[str] = []
        self._inbox: list[tuple[int, WorkflowEventPayload]] = []
        self._results: dict[int, tuple[str, str]] = {}
        self._event_counter = 0
        self._current_event_id: int | None = None
        self._cancel_requested = False
        self._cancel_reason = ""
        self._cancel_actor = "system"
        self._last_checks: dict[str, bool] | None = None
        self._last_confidence: float | None = None
        self._exec_guard: GuardInputs | None = None
        self._plan_guard: GuardInputs | None = None
        self._params: WorkflowParams | None = None
        self._failure_reason: str | None = None

    # ------------------------------------------------------------------ #
    # Main durable body                                                   #
    # ------------------------------------------------------------------ #
    @workflow.run
    async def run(self, params: WorkflowParams) -> WorkflowResult:
        self._params = params
        self._evaluator = TransitionEvaluator(_DEFINITIONS[params.definition_name])
        try:
            # created → spec_drafting → clarification → spec_review
            await self._auto("generate_spec_draft")
            await self._auto("gather_clarifications")
            await self._auto("submit_spec_for_review")

            await self._create_approval("spec")
            while True:
                ev = await self._await_gate([EVENT_SPEC_APPROVED, EVENT_SPEC_CHANGES])
                if ev.type == EVENT_SPEC_CHANGES:
                    await self._apply_event(ev)  # spec_review → clarification
                    await self._auto("submit_spec_for_review")  # → spec_review
                    await self._create_approval("spec")
                    continue
                await self._apply_event(ev)  # → spec_approved
                break

            # spec_approved → plan_drafting → plan_review
            await self._auto("generate_plan")
            await self._auto("submit_plan_for_review")

            # plan gate is policy-conditional (load_guard_inputs).
            self._plan_guard = await self._load_guard_inputs("plan")
            if self._plan_guard.plan_required:
                await self._create_approval("plan")
                ev = await self._await_gate([EVENT_PLAN_APPROVED])
                await self._apply_event(ev)  # plan_review → task_generation
            else:
                await self._apply_event(
                    WorkflowEventPayload(
                        type=EVENT_PLAN_APPROVED,
                        actor="system",
                        payload={"plan_not_required": True},
                    )
                )

            # task_generation → task_ready
            await self._auto("generate_tasks")

            await self._execute_loop()
            if self._state in _TERMINAL:
                return self._result()

            # pr_opened → awaiting_review
            await self._auto("request_reviews")

            # merge gate (deterministic merge_ready over the Update payload).
            await self._create_approval("pr")
            while True:
                ev = await self._await_gate([EVENT_REVIEW_APPROVED])
                try:
                    await self._apply_event(ev)  # awaiting_review → merged
                    break
                except GuardFailedError:
                    continue  # error already returned to the Update; await a fix

            # merged → closed
            await self._auto("close_task")
            return self._result()
        except _Cancelled:
            return await self._do_cancel()

    # ------------------------------------------------------------------ #
    # Execute / verify / retry loop                                       #
    # ------------------------------------------------------------------ #
    async def _execute_loop(self) -> None:
        assert self._params is not None
        self._exec_guard = await self._load_guard_inputs("execute")

        # Durable wait until execute preconditions are met (re-poll guard inputs).
        while not self._preconditions_met():
            self._check_cancel()
            await self._sleep(self._params.retry_policy.initial_delay_seconds)
            self._exec_guard = await self._load_guard_inputs("execute")

        # task_ready → executing (first entry)
        await self._advance("start_agent_run")

        retries_done = 0
        while True:
            self._retry_count = retries_done
            agent = await self._run_agent(retries_done)

            while agent.status == "awaiting_input":
                # executing → needs_human_input (DSL low_confidence edge)
                await self._advance(
                    "low_confidence",
                    confidence=0.0,
                    payload={"reason": agent.needs_human_reason or "awaiting input"},
                )
                await self._notify("pause_and_notify", agent.needs_human_reason or "awaiting input")
                ev = await self._await_gate([EVENT_RESUME])
                # needs_human_input → executing (durable-engine control transition)
                await self._control(
                    WorkflowState.EXECUTING, EVENT_RESUME, ["resume_agent"], actor=ev.actor
                )
                self._resolve_pending_ok()
                agent = await self._resume_agent(retries_done)

            if agent.status == "failed":
                await self._advance(
                    "low_confidence", confidence=0.0, payload={"reason": "agent failed"}
                )
                await self._notify("pause_and_notify", "agent run failed")
                self._failure_reason = "agent_failed"
                return

            self._last_confidence = agent.confidence

            # executing → verifying
            checks = await self._run_checks(retries_done)
            self._last_checks = checks.results
            await self._advance("run_checks")

            if checks.all_passed:
                # verifying → pr_opened (open_pr_with_spec_traceability)
                await self._advance("all_checks_passed")
                await self._open_pr(agent.branch_name, retries_done)
                return

            # Failure: the evaluator is the single source of truth for whether the
            # retry budget remains (→ executing + backoff) or is exhausted
            # (→ needs_human_input). ``retry_count`` == retries already taken.
            self._retry_count = retries_done
            delay = _backoff_seconds(
                self._params.retry_policy.initial_delay_seconds,
                self._params.retry_policy.backoff,
                retries_done,
            )
            await self._advance(
                "checks_failed",
                payload={"backoff_seconds": delay, "retry_count": retries_done + 1},
            )
            if self._state == WorkflowState.NEEDS_HUMAN_INPUT:
                await self._notify("pause_and_notify", "retry budget exhausted")
                self._failure_reason = "retry_budget_exhausted"
                return

            # verifying → executing (retry_budget_remaining): durable backoff.
            await self._sleep(delay)
            retries_done += 1

    def _preconditions_met(self) -> bool:
        if self._exec_guard is None:
            return False
        required = ["repo_target_set", "policy_loaded", "skill_profile_set", "knowledge_synced"]
        return all(self._exec_guard.preconditions.get(p, False) for p in required)

    # ------------------------------------------------------------------ #
    # Updates / Signals / Queries                                         #
    # ------------------------------------------------------------------ #
    @workflow.update
    async def submit_event(self, event: WorkflowEventPayload) -> str:
        """Synchronously apply a human/agent gate event; returns the new state."""
        self._event_counter += 1
        eid = self._event_counter
        self._inbox.append((eid, event))
        await workflow.wait_condition(lambda: eid in self._results)
        kind, value = self._results.pop(eid)
        if kind == "error":
            error_type, _, message = value.partition("|")
            raise ApplicationError(message or value, type=error_type or "WorkflowError")
        return value

    @submit_event.validator
    def _validate_event(self, event: WorkflowEventPayload) -> None:
        if event.type == EVENT_CANCEL:
            raise ApplicationError(
                "cancel is delivered as a Signal (cancel_run), not an Update",
                type="InvalidTransitionError",
            )
        if event.type not in self._awaiting:
            raise ApplicationError(
                f"no enabled transition from {self._state.value!r} on event {event.type!r}; "
                f"allowed={self._awaiting}",
                type="InvalidTransitionError",
            )

    @workflow.signal
    async def cancel_run(self, reason: str = "cancelled") -> None:
        self._cancel_requested = True
        self._cancel_reason = reason or "cancelled"

    @workflow.query
    def current_state(self) -> str:
        return self._state.value

    @workflow.query
    def awaiting(self) -> list[str]:
        return list(self._awaiting)

    @workflow.query
    def transition_count(self) -> int:
        return self._seq

    # ------------------------------------------------------------------ #
    # Gate / event helpers                                                #
    # ------------------------------------------------------------------ #
    async def _await_gate(self, allowed: list[str]) -> WorkflowEventPayload:
        self._awaiting = list(allowed)
        await workflow.wait_condition(lambda: self._cancel_requested or self._has_event(allowed))
        if self._cancel_requested and not self._has_event(allowed):
            raise _Cancelled()
        eid, ev = self._pop_event(allowed)
        self._current_event_id = eid
        return ev

    def _has_event(self, allowed: list[str]) -> bool:
        return any(ev.type in allowed for _, ev in self._inbox)

    def _pop_event(self, allowed: list[str]) -> tuple[int, WorkflowEventPayload]:
        for i, (eid, ev) in enumerate(self._inbox):
            if ev.type in allowed:
                del self._inbox[i]
                return eid, ev
        raise RuntimeError("no matching event")  # pragma: no cover

    async def _apply_event(self, ev: WorkflowEventPayload) -> None:
        ctx = self._pure_ctx(ev)
        try:
            decision = self._evaluator.resolve(self._state, ev.type, pure_guard_ctx=ctx)
        except (InvalidTransitionError, GuardFailedError, PreconditionError) as exc:
            if self._current_event_id is not None:
                self._results[self._current_event_id] = ("error", _err_payload(exc))
                self._current_event_id = None
            raise
        await self._persist(decision, ev.type, actor=ev.actor, payload=ev.payload)
        self._awaiting = []
        if self._current_event_id is not None:
            self._results[self._current_event_id] = ("ok", self._state.value)
            self._current_event_id = None

    def _resolve_pending_ok(self) -> None:
        """Resolve a pending Update for a gate event handled via a control
        transition (e.g. ``resume``), which bypasses ``_apply_event``."""
        self._awaiting = []
        if self._current_event_id is not None:
            self._results[self._current_event_id] = ("ok", self._state.value)
            self._current_event_id = None

    def _check_cancel(self) -> None:
        if self._cancel_requested:
            raise _Cancelled()

    # ------------------------------------------------------------------ #
    # Transition application                                              #
    # ------------------------------------------------------------------ #
    async def _auto(self, event: str) -> None:
        """Apply a non-gated action transition (system-driven)."""
        self._check_cancel()
        await self._advance(event)

    async def _advance(
        self,
        event: str,
        *,
        confidence: float | None = None,
        payload: dict[str, Any] | None = None,
        actor: str = "system",
    ) -> None:
        ctx = self._pure_ctx_system(confidence=confidence)
        decision = self._evaluator.resolve(self._state, event, pure_guard_ctx=ctx)
        await self._persist(decision, event, actor=actor, payload=payload or {})

    async def _control(
        self,
        to_state: WorkflowState,
        event: str,
        effects: list[str],
        *,
        actor: str = "system",
        payload: dict[str, Any] | None = None,
        record: str | None = None,
    ) -> None:
        """A durable-engine control transition not present in the DSL (resume/cancel)."""
        rec = self._record(
            from_state=self._state,
            to_state=to_state,
            event=event,
            guard_results={},
            effects=effects,
            record=record,
            actor=actor,
            payload=payload or {},
            skill=None,
        )
        await self._persist_record(rec, to_state)

    async def _persist(
        self,
        decision: Any,
        event: str,
        *,
        actor: str,
        payload: dict[str, Any],
    ) -> None:
        rec = self._record(
            from_state=self._state,
            to_state=decision.to_state,
            event=event,
            guard_results=decision.guard_results,
            effects=decision.effects,
            record=decision.record,
            actor=actor,
            payload=payload,
            skill=decision.skill,
        )
        await self._persist_record(rec, decision.to_state)

    def _record(
        self,
        *,
        from_state: WorkflowState,
        to_state: WorkflowState,
        event: str,
        guard_results: dict[str, bool],
        effects: list[str],
        record: str | None,
        actor: str,
        payload: dict[str, Any],
        skill: str | None,
    ) -> TransitionRecord:
        assert self._params is not None
        key = transition_idempotency_key(self._params.workflow_run_id, self._seq + 1)
        return TransitionRecord(
            workflow_run_id=self._params.workflow_run_id,
            workspace_id=self._params.workspace_id,
            from_state=from_state,
            to_state=to_state,
            event=event,
            idempotency_key=key,
            guard_results=guard_results,
            effects_dispatched=effects,
            record=record,
            actor=actor,
            skill=skill,
            payload=payload,
            temporal_run_id=workflow.info().run_id,
        )

    async def _persist_record(self, rec: TransitionRecord, to_state: WorkflowState) -> None:
        await workflow.execute_activity(
            "forge.persist_transition",
            arg=rec,
            start_to_close_timeout=_PERSIST_TIMEOUT,
            retry_policy=_PERSIST_RETRY,
            result_type=int,
        )
        self._seq += 1
        self._state = to_state

    # ------------------------------------------------------------------ #
    # Pure-guard context construction                                     #
    # ------------------------------------------------------------------ #
    def _pure_ctx_system(self, *, confidence: float | None = None) -> PureGuardContext:
        assert self._params is not None
        return PureGuardContext(
            retry_count=self._retry_count,
            max_retries=self._params.retry_policy.max_retries,
            checks=self._last_checks,
            confidence=confidence if confidence is not None else self._last_confidence,
            confidence_threshold=self._params.escalation_policy.confidence_threshold,
            preconditions=(self._exec_guard.preconditions if self._exec_guard else {}),
            plan_required=(self._plan_guard.plan_required if self._plan_guard else True),
        )

    def _pure_ctx(self, ev: WorkflowEventPayload) -> PureGuardContext:
        ctx = self._pure_ctx_system(confidence=ev.confidence)
        if ev.type == EVENT_REVIEW_APPROVED:
            ctx.merge_signals = {
                "review_approved_by_human": True,
                "ci_status_green": bool(ev.payload.get("ci_status_green", False)),
                "spec_validated": bool(ev.payload.get("spec_validated", False)),
            }
        return ctx

    # ------------------------------------------------------------------ #
    # Activity wrappers                                                   #
    # ------------------------------------------------------------------ #
    async def _load_guard_inputs(self, phase: str) -> GuardInputs:
        assert self._params is not None
        return await workflow.execute_activity(
            "forge.load_guard_inputs",
            arg=GuardInputsRequest(
                workflow_run_id=self._params.workflow_run_id,
                workspace_id=self._params.workspace_id,
                phase=phase,
            ),
            start_to_close_timeout=_GUARD_TIMEOUT,
            retry_policy=_THRICE,
            result_type=GuardInputs,
        )

    async def _run_agent(self, attempt: int) -> AgentRunResultDTO:
        assert self._params is not None
        return await workflow.execute_activity(
            "forge.run_agent",
            arg=RunAgentInput(
                workflow_run_id=self._params.workflow_run_id,
                task_id=self._params.task_id,
                workspace_id=self._params.workspace_id,
                attempt=attempt,
                idempotency_key=f"{self._params.workflow_run_id}:run_agent:{attempt}",
            ),
            start_to_close_timeout=_DEFAULT_AGENT_TIMEOUT,
            heartbeat_timeout=_AGENT_HEARTBEAT,
            retry_policy=_ONCE,
            result_type=AgentRunResultDTO,
        )

    async def _resume_agent(self, attempt: int) -> AgentRunResultDTO:
        assert self._params is not None
        from forge_workflow.temporal.payloads import ResumeAgentInput

        return await workflow.execute_activity(
            "forge.resume_agent",
            arg=ResumeAgentInput(
                workflow_run_id=self._params.workflow_run_id,
                task_id=self._params.task_id,
                workspace_id=self._params.workspace_id,
                agent_run_id=None,
                attempt=attempt,
                idempotency_key=f"{self._params.workflow_run_id}:resume_agent:{attempt}",
            ),
            start_to_close_timeout=_DEFAULT_AGENT_TIMEOUT,
            heartbeat_timeout=_AGENT_HEARTBEAT,
            retry_policy=_ONCE,
            result_type=AgentRunResultDTO,
        )

    async def _run_checks(self, attempt: int) -> ChecksResult:
        assert self._params is not None
        return await workflow.execute_activity(
            "forge.run_checks",
            arg=RunChecksInput(
                workflow_run_id=self._params.workflow_run_id,
                workspace_id=self._params.workspace_id,
                attempt=attempt,
                idempotency_key=f"{self._params.workflow_run_id}:run_checks:{attempt}",
            ),
            start_to_close_timeout=_CHECKS_TIMEOUT,
            retry_policy=_THRICE,
            result_type=ChecksResult,
        )

    async def _open_pr(self, branch_name: str | None, attempt: int) -> None:
        assert self._params is not None
        await workflow.execute_activity(
            "forge.open_pr_with_spec_traceability",
            arg=OpenPrInput(
                workflow_run_id=self._params.workflow_run_id,
                workspace_id=self._params.workspace_id,
                branch_name=branch_name,
                idempotency_key=f"{self._params.workflow_run_id}:open_pr",
            ),
            start_to_close_timeout=_PR_TIMEOUT,
            retry_policy=_THRICE,
        )

    async def _notify(self, kind: str, reason: str) -> None:
        assert self._params is not None
        await workflow.execute_activity(
            "forge.pause_and_notify",
            arg=NotifyInput(
                workflow_run_id=self._params.workflow_run_id,
                workspace_id=self._params.workspace_id,
                reason=reason,
                kind=kind,
                idempotency_key=f"{self._params.workflow_run_id}:notify:{self._seq}",
            ),
            start_to_close_timeout=_NOTIFY_TIMEOUT,
            retry_policy=_FIVE,
        )

    async def _create_approval(self, gate: str) -> None:
        assert self._params is not None
        await workflow.execute_activity(
            "forge.create_approval",
            arg=ApprovalInput(
                workflow_run_id=self._params.workflow_run_id,
                workspace_id=self._params.workspace_id,
                task_id=self._params.task_id,
                gate=gate,
                summary=f"{gate} approval for run {self._params.workflow_run_id}",
                idempotency_key=f"{self._params.workflow_run_id}:approval:{gate}:{self._seq}",
            ),
            start_to_close_timeout=_APPROVAL_TIMEOUT,
            retry_policy=_PERSIST_RETRY,
            result_type=str,
        )

    async def _do_cancel(self) -> WorkflowResult:
        assert self._params is not None
        await workflow.execute_activity(
            "forge.cleanup_worktree",
            arg=CleanupInput(
                workflow_run_id=self._params.workflow_run_id,
                workspace_id=self._params.workspace_id,
                reason=self._cancel_reason,
                idempotency_key=f"{self._params.workflow_run_id}:cleanup",
            ),
            start_to_close_timeout=_CLEANUP_TIMEOUT,
            retry_policy=_THRICE,
        )
        await self._control(
            WorkflowState.CANCELLED,
            EVENT_CANCEL,
            ["cleanup_worktree"],
            actor=self._cancel_actor,
            payload={"reason": self._cancel_reason},
        )
        self._failure_reason = self._cancel_reason
        return self._result()

    # ------------------------------------------------------------------ #
    # Durable timer + result                                              #
    # ------------------------------------------------------------------ #
    async def _sleep(self, seconds: int) -> None:
        """Durable backoff timer (survives restart; fast-forwarded by time-skip).

        Uses the canonical ``workflow.sleep`` so the time-skipping test server
        advances it instantly and a real deployment resumes it after a crash.
        Cancel is honoured at the next gate / loop checkpoint after the delay.
        """
        if seconds <= 0:
            return
        await workflow.sleep(seconds)
        self._check_cancel()

    def _result(self) -> WorkflowResult:
        return WorkflowResult(
            final_state=self._state,
            transition_count=self._seq,
            failure_reason=self._failure_reason,
        )


_TERMINAL = {
    WorkflowState.NEEDS_HUMAN_INPUT,
    WorkflowState.FAILED,
    WorkflowState.CANCELLED,
}


def _err_payload(exc: WorkflowError) -> str:
    return f"{type(exc).__name__}|{exc}"


__all__ = ["FeatureWorkflow"]
