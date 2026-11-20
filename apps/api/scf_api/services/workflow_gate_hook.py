"""Feature-run gates resolve through approvals (spec K §4.7, P0).

Entering ``spec_review`` / ``plan_review`` / ``awaiting_review`` opens an
approval request for the run; this module is the other half: when that request
resolves, a gate hook submits the matching ``*_by_human`` event straight to the
run's engine (never through the HTTP ``/transition`` route, which now refuses
those events), with the deciding user as the transition actor — so the
approval service's rules (required approvals, no self-approval, attestation)
are the only way a feature run passes a gate (ADR-0005).

A ``plan`` or ``pr`` request resolved ``rejected`` or ``changes_requested``
ends the run: the hook cancels it with the decider as actor and the reason
``gate_<gate>_<status>`` (spec K §4.11); ``spec`` keeps its
``spec_changes_requested`` edge.

Failure contract: a failed submit is logged, recorded on the request
(``gate_payload.workflow_event``) and in the audit log (an ``approval`` entry
with status ``workflow_event_failed``), never raised, and never rolls back the
recorded decision; a failed audit write is itself only logged.
``POST /workflow/runs/{id}/gates/{gate}/sync`` is the recovery path and shares
:func:`submit_gate_event` with the hook; applying the event there adds a
``workflow_event_synced`` entry naming who ran the sync. When the
wrapped hook (the ``pr`` attestation) raises, the gate is ``blocked``: no event
is submitted and the error reaches the approval service (which reports it).
Sync merges an approved ``pr`` gate only on proof of attestation
(:func:`attested_or_blocked`), so it is also the blocked gate's recovery route.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import TYPE_CHECKING, Any, ClassVar

from scf_approval.audit import ApprovalAuditSink
from scf_approval.models import (
    ApprovalAction,
    ApprovalDecisionRequest,
    ApprovalRequest,
    GateStatus,
    GateType,
    Principal,
    ResolutionOutcome,
)
from scf_approval.registry import GateResolutionHook
from scf_contracts import WorkflowState
from scf_contracts.automation import HUMAN_GATE_EVENTS
from scf_db.attest.repository import AttestationRepository

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from scf_approval import ApprovalService

logger = logging.getLogger(__name__)

#: The subject type every feature-run gate request carries (A1/A2).
WORKFLOW_RUN_SUBJECT = "workflow_run"
#: The subject type of a line's human-station gate: the visit (spec K §4.2).
STEP_SUBJECT = "step"

EVENT_SPEC_CHANGES = "spec_changes_requested"
EVENT_CANCEL = "cancel"

#: ``gate_payload.workflow_event.status`` of a gate whose wrapped hook failed.
GATE_BLOCKED = "blocked"

#: Audit status of a resolved gate whose event the run's engine refused.
SUBMIT_FAILED = "workflow_event_failed"
#: Audit status of a resolved gate whose event gate sync applied later.
SUBMIT_SYNCED = "workflow_event_synced"

#: Events only an approval resolution may produce. ``/transition`` refuses all
#: four; automations already refuse the three ``*_by_human`` approvals.
GATE_EVENTS: frozenset[str] = HUMAN_GATE_EVENTS | {EVENT_SPEC_CHANGES}

#: Gate -> the event an ``approved`` resolution submits.
APPROVED_EVENT_BY_GATE: dict[GateType, str] = {
    GateType.SPEC: "spec_approved_by_human",
    GateType.PLAN: "plan_approved_by_human",
    GateType.PR: "review_approved_by_human",
}

#: Gate -> the run state that is waiting at that gate.
WAITING_STATE_BY_GATE: dict[GateType, str] = {
    GateType.SPEC: WorkflowState.SPEC_REVIEW.value,
    GateType.PLAN: WorkflowState.PLAN_REVIEW.value,
    GateType.PR: WorkflowState.AWAITING_REVIEW.value,
}

#: Run state -> the gate opened when a V1 run lands in it.
GATE_BY_STATE: dict[str, GateType] = {state: gate for gate, state in WAITING_STATE_BY_GATE.items()}

#: Merge-guard signals a ``pr`` gate payload may carry for the engines. They
#: are forwarded when present, never invented: a missing signal fails the
#: guard, the failure is recorded, and gate sync retries it later.
_MERGE_SIGNALS = ("ci_status_green", "spec_validated")

#: ``workspace_id -> context manager yielding that workspace's engine``. The
#: default opens its own session and commits when the block exits cleanly;
#: tests substitute the engine their runs live in.
EngineFactory = Callable[[uuid.UUID], AbstractContextManager[Any]]


def agent_actor_for_run(run_id: uuid.UUID) -> str:
    """The run's agent identity: gate requests are opened *by the run's agent
    work*, which acts under the run id. No human user id can equal it, and the
    authorizer's agent-author rule keys on this ``agent:<id>`` form."""
    return f"agent:{run_id}"


def event_for_resolution(gate_type: GateType, status: GateStatus) -> str | None:
    """The workflow event a resolved gate submits (``None`` = no event).

    ``approved`` submits the gate's ``*_by_human`` event. ``rejected`` /
    ``changes_requested`` submits ``spec_changes_requested`` on ``spec`` and
    ``cancel`` on ``plan`` / ``pr`` (the run ends; rework is a new run). Expiry
    submits nothing.
    """
    if status is GateStatus.APPROVED:
        return APPROVED_EVENT_BY_GATE.get(gate_type)
    if status in (GateStatus.REJECTED, GateStatus.CHANGES_REQUESTED):
        if gate_type is GateType.SPEC:
            return EVENT_SPEC_CHANGES
        if gate_type in (GateType.PLAN, GateType.PR):
            return EVENT_CANCEL
    return None


def cancel_reason(request: ApprovalRequest) -> str:
    """Why a gate resolution cancelled its run, e.g. ``gate_plan_rejected``."""
    return f"gate_{request.gate_type.value}_{request.status.value}"


def mark_blocked(request: ApprovalRequest, *, event: str | None, actor_ref: str) -> None:
    """Record on ``request`` that its attestation failed, so its run must not move."""
    request.gate_payload = {
        **request.gate_payload,
        "workflow_event": {
            "status": GATE_BLOCKED,
            "reason": "attestation_failed",
            "event": event,
            "actor": actor_ref,
        },
    }


async def attested_or_blocked(
    approvals: ApprovalService, session: Session, request: ApprovalRequest, actor: Principal
) -> bool:
    """Gate sync for an approved ``pr`` gate: True once an attestation proves it.

    An attestation already recorded for the run is the proof (it is never made
    twice). Otherwise the wrapped attestation hook (the one the gate hook runs)
    runs now; when it fails, ``request`` is marked blocked, that is persisted,
    and the answer is False: the run must not merge.
    """
    assert request.workflow_run_id is not None
    existing = AttestationRepository(session).get_by_run(
        request.workspace_id, workflow_run_id=request.workflow_run_id
    )
    if existing is not None:
        return True
    hook = approvals.resolution_hook(GateType.PR)
    try:
        if not isinstance(hook, _WorkflowRunGateHook) or hook._wrapped is None:
            raise LookupError("the pr gate wraps no attestation hook")
        approve = ApprovalDecisionRequest(decision=ApprovalAction.APPROVE)
        outcome = await hook._wrapped.on_resolved(request, approve, actor)
        if not outcome.completed:
            raise RuntimeError(f"attestation did not complete: {outcome.blocking_reasons}")
    except Exception:
        logger.exception(
            "attesting pr gate %s for run %s failed; the gate stays blocked",
            request.id,
            request.workflow_run_id,
        )
        mark_blocked(
            request,
            event=event_for_resolution(request.gate_type, request.status),
            actor_ref=actor.actor_ref,
        )
        await approvals.save_gate_payload(request)
        return False
    return True


def mark_submitted(request: ApprovalRequest, *, event: str, actor_ref: str, new_state: str) -> None:
    """Record on ``request`` that its run passed the gate: the event was applied."""
    submitted: dict[str, Any] = {
        "status": "submitted",
        "event": event,
        "actor": actor_ref,
        "to_state": new_state,
    }
    if event == EVENT_CANCEL:
        submitted["reason"] = cancel_reason(request)
    request.gate_payload = {**request.gate_payload, "workflow_event": submitted}


class GateBlocked(Exception):
    """An approved ``pr`` gate with no attestation to prove it: the run must not merge."""


async def sync_gate_event(
    engine: Any,
    approvals: ApprovalService,
    session: Session,
    request: ApprovalRequest,
    *,
    run_state: str,
    fallback_user_id: uuid.UUID,
) -> str | None:
    """Apply a resolved gate's event to a run still waiting there, and record it.

    The core of ``POST /workflow/runs/{id}/gates/{gate}/sync`` (the recovery for a
    hook whose submit failed). Returns the run's new state, or ``None`` when there
    was nothing to apply: the gate is pending, or the run already left it. An
    approved ``pr`` gate merges only on proof of attestation; without one it raises
    :class:`GateBlocked`. Once applied, the request's ``workflow_event`` reads
    ``submitted``, so it no longer shows the gate as unsynced, and the approval
    service's audit log gets a ``workflow_event_synced`` entry naming
    ``fallback_user_id`` (the user running the sync, and the transition actor
    only when the request names no resolver).
    """
    gate = request.gate_type
    event = event_for_resolution(gate, request.status)
    if (
        request.status is GateStatus.PENDING
        or run_state != WAITING_STATE_BY_GATE.get(gate)
        or event is None
    ):
        return None
    resolver = request.resolver_user_id or fallback_user_id
    actor_ref = f"user:{resolver}"
    if gate is GateType.PR and request.status is GateStatus.APPROVED:
        decider = Principal(kind="user", id=resolver, workspace_id=request.workspace_id)
        if not await attested_or_blocked(approvals, session, request, decider):
            raise GateBlocked(f"pr gate {request.id} has no attestation")
    new_state = await submit_gate_event(engine, request=request, actor_ref=actor_ref)
    mark_submitted(request, event=event, actor_ref=actor_ref, new_state=new_state)
    await approvals.save_gate_payload(request)
    _audit_gate_event(
        approvals.audit_sink,
        request,
        status=SUBMIT_SYNCED,
        actor=f"user:{fallback_user_id}",
        metadata={"event": event, "transition_actor": actor_ref, "to_state": new_state},
    )
    return new_state


def _audit_gate_event(
    audit: ApprovalAuditSink,
    request: ApprovalRequest,
    *,
    status: str,
    actor: str,
    metadata: dict[str, Any],
    detail: str | None = None,
) -> None:
    """Audit what happened to ``request``'s workflow event. A failed write is
    logged, never raised: the decision and the run's move already stand."""
    try:
        audit.record_approval(
            request.gate_type.value,
            status=status,
            actor=actor,
            run_id=request.workflow_run_id,
            request_id=request.id,
            workspace_id=request.workspace_id,
            detail=detail,
            metadata=metadata,
        )
    except Exception:
        logger.exception(
            "auditing %s for gate %s of run %s failed", status, request.id, request.workflow_run_id
        )


def _merge_signals(request: ApprovalRequest) -> dict[str, bool]:
    # Only the boolean true counts: bool() would read the string "false" as true.
    return {
        key: request.gate_payload[key] is True
        for key in _MERGE_SIGNALS
        if key in request.gate_payload
    }


async def submit_gate_event(engine: Any, *, request: ApprovalRequest, actor_ref: str) -> str:
    """Submit ``request``'s resolved event to its run's engine; return new state.

    Engine-direct on both engines: the Temporal engine's async
    ``atransition`` (a Workflow Update carrying ``actor`` into the transition
    record) when present, else the V1 FSM's ``transition`` (which records
    ``last_actor``). Raises whatever the engine raises — callers decide how to
    record the failure (the hook swallows + stamps; gate sync surfaces 409).
    """
    event = event_for_resolution(request.gate_type, request.status)
    if event is None:
        raise ValueError(
            f"gate '{request.gate_type.value}' resolved as '{request.status.value}' "
            "submits no workflow event"
        )
    run_id = request.workflow_run_id
    if run_id is None:
        raise ValueError(f"approval request {request.id} carries no workflow_run_id")
    if event == EVENT_CANCEL:
        return await _cancel_run(engine, run_id, actor_ref=actor_ref, reason=cancel_reason(request))
    signals = _merge_signals(request)
    atransition = getattr(engine, "atransition", None)
    if atransition is not None:
        state = await atransition(run_id, event, actor=actor_ref, **signals)
        return state.value if hasattr(state, "value") else str(state)
    if signals and hasattr(engine, "update_context"):
        engine.update_context(run_id, signals)
    state = engine.transition(run_id, event, actor=actor_ref)
    return state.value if hasattr(state, "value") else str(state)


async def _cancel_run(engine: Any, run_id: uuid.UUID, *, actor_ref: str, reason: str) -> str:
    """Cancel ``run_id`` as ``actor_ref``; an already-finished V1 run is left as it is."""
    atransition = getattr(engine, "atransition", None)
    if atransition is not None:
        state = await atransition(run_id, EVENT_CANCEL, actor=actor_ref, reason=reason)
        return state.value if hasattr(state, "value") else str(state)
    return str(engine.cancel(run_id, actor=actor_ref, reason=reason).current_state)


@contextmanager
def _default_engine_factory(workspace_id: uuid.UUID) -> Iterator[Any]:
    """The workspace's engine on a session of its own, like the attestation
    hook (the deciding request's session belongs to the approval service)."""
    from scf_api.db import get_session_factory
    from scf_api.services.workflow_engine import select_workflow_engine

    with get_session_factory()() as session:
        yield select_workflow_engine(session=session, workspace_id=workspace_id)
        session.commit()


class _WorkflowRunGateHook:
    """Wrap a gate's existing hook; then advance the feature run, if any.

    The wrapped hook runs first, unchanged (for ``pr`` that is the changeset
    attestation — its outcome and any exception behave exactly as before).
    Only a request opened for a workflow run (``subject_type='workflow_run'``
    with a ``workflow_run_id``) submits an event. A line's human-station request
    (``subject_type='step'``) only sends its run the ``station_gate_changed``
    hint, which carries nothing the run trusts (spec K §4.2). Every other gate
    resolves exactly as it did before this hook existed.
    """

    gate_type: ClassVar[GateType]

    def __init__(
        self,
        *,
        wrapped: GateResolutionHook | None = None,
        engine_factory: EngineFactory | None = None,
        audit: ApprovalAuditSink,
    ) -> None:
        self._wrapped = wrapped
        self._engine_factory = engine_factory or _default_engine_factory
        self._audit = audit

    async def _hint_station(self, request: ApprovalRequest) -> None:
        """Wake the line run at this gate; it reads the decision itself (spec K §4.2)."""
        try:
            with self._engine_factory(request.workspace_id) as engine:
                await engine.astation_gate_changed(request.workflow_run_id, request.id)
        except Exception:  # never raised: the decision stands and the run wakes itself
            logger.exception(
                "hinting line run %s that gate %s was decided failed; "
                "the run reads its gate within 15 minutes",
                request.workflow_run_id,
                request.id,
            )

    async def on_resolved(
        self,
        request: ApprovalRequest,
        decision: ApprovalDecisionRequest,
        actor: Principal,
        *,
        session: Any = None,
    ) -> ResolutionOutcome:
        for_run = (
            request.subject_type == WORKFLOW_RUN_SUBJECT and request.workflow_run_id is not None
        )
        step = request.subject_type == STEP_SUBJECT and request.workflow_run_id is not None
        event = event_for_resolution(request.gate_type, request.status)
        if self._wrapped is not None:
            try:
                outcome = await self._wrapped.on_resolved(request, decision, actor, session=session)
            except Exception:
                if for_run or step:
                    # A failed pr attestation blocks the gate: the run never moves on it.
                    mark_blocked(request, event=event, actor_ref=actor.actor_ref)
                if step:
                    await self._hint_station(request)
                raise
        else:
            outcome = ResolutionOutcome(
                completed=True, follow_up_state=f"{request.gate_type.value}_{request.status.value}"
            )

        if step:
            attestation = outcome.details.get("attestation_id")
            if outcome.completed and attestation:
                # A line's pr station takes an approval only with this proof.
                request.gate_payload = {**request.gate_payload, "attestation_id": attestation}
            elif self._wrapped is not None and request.status == GateStatus.APPROVED:
                mark_blocked(request, event=event, actor_ref=actor.actor_ref)
            await self._hint_station(request)
            return outcome
        if not for_run or event is None:
            return outcome
        if (
            self._wrapped is not None
            and not outcome.completed
            and request.status == GateStatus.APPROVED
        ):
            # An attestation that did not complete is no proof: block exactly as if it raised.
            mark_blocked(request, event=event, actor_ref=actor.actor_ref)
            return outcome

        try:
            with self._engine_factory(request.workspace_id) as engine:
                new_state = await submit_gate_event(
                    engine, request=request, actor_ref=actor.actor_ref
                )
        except Exception as exc:  # never raised: the decision stands (A3)
            logger.exception(
                "workflow gate %s for run %s resolved %s but submitting %r failed; "
                "recover with POST /workflow/runs/%s/gates/%s/sync",
                request.id,
                request.workflow_run_id,
                request.status.value,
                event,
                request.workflow_run_id,
                request.gate_type.value,
            )
            error = f"{type(exc).__name__}: {exc}"
            request.gate_payload = {
                **request.gate_payload,
                "workflow_event": {
                    "status": "failed",
                    "event": event,
                    "actor": actor.actor_ref,
                    "error": error,
                },
            }
            _audit_gate_event(
                self._audit,
                request,
                status=SUBMIT_FAILED,
                actor=actor.actor_ref,
                detail=error,
                metadata={"event": event},
            )
            outcome.details = {
                **outcome.details,
                "workflow_event": event,
                "workflow_event_error": error,
            }
            if self._wrapped is None:
                outcome.completed = False
                outcome.blocking_reasons = [
                    *outcome.blocking_reasons,
                    f"submitting workflow event '{event}' failed; "
                    "the decision is recorded and the run can be synced",
                ]
            return outcome

        mark_submitted(request, event=event, actor_ref=actor.actor_ref, new_state=new_state)
        outcome.details = {
            **outcome.details,
            "workflow_event": event,
            "workflow_state": new_state,
        }
        if self._wrapped is None:
            outcome.completed = True
            outcome.follow_up_state = new_state
        return outcome


class SpecWorkflowGateHook(_WorkflowRunGateHook):
    gate_type: ClassVar[GateType] = GateType.SPEC


class PlanWorkflowGateHook(_WorkflowRunGateHook):
    gate_type: ClassVar[GateType] = GateType.PLAN


class PrWorkflowGateHook(_WorkflowRunGateHook):
    gate_type: ClassVar[GateType] = GateType.PR


__all__ = [
    "APPROVED_EVENT_BY_GATE",
    "EVENT_CANCEL",
    "GATE_BY_STATE",
    "GATE_EVENTS",
    "STEP_SUBJECT",
    "WAITING_STATE_BY_GATE",
    "WORKFLOW_RUN_SUBJECT",
    "EngineFactory",
    "GateBlocked",
    "PlanWorkflowGateHook",
    "PrWorkflowGateHook",
    "SpecWorkflowGateHook",
    "agent_actor_for_run",
    "attested_or_blocked",
    "cancel_reason",
    "event_for_resolution",
    "mark_blocked",
    "mark_submitted",
    "submit_gate_event",
    "sync_gate_event",
]
