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

Failure contract: a failed submit is logged and recorded on the request
(``gate_payload.workflow_event``), never raised, and never rolls back the
recorded decision. ``POST /workflow/runs/{id}/gates/{gate}/sync`` is the
recovery path and shares :func:`submit_gate_event` with the hook. When the
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

from forge_approval.models import (
    ApprovalAction,
    ApprovalDecisionRequest,
    ApprovalRequest,
    GateStatus,
    GateType,
    Principal,
    ResolutionOutcome,
)
from forge_approval.registry import GateResolutionHook
from forge_contracts import WorkflowState
from forge_contracts.automation import HUMAN_GATE_EVENTS
from forge_db.attest.repository import AttestationRepository

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from forge_approval import ApprovalService

logger = logging.getLogger(__name__)

#: The subject type every feature-run gate request carries (A1/A2).
WORKFLOW_RUN_SUBJECT = "workflow_run"

EVENT_SPEC_CHANGES = "spec_changes_requested"
EVENT_CANCEL = "cancel"

#: ``gate_payload.workflow_event.status`` of a gate whose wrapped hook failed.
GATE_BLOCKED = "blocked"

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
    from forge_api.db import get_session_factory
    from forge_api.services.workflow_engine import select_workflow_engine

    with get_session_factory()() as session:
        yield select_workflow_engine(session=session, workspace_id=workspace_id)
        session.commit()


class _WorkflowRunGateHook:
    """Wrap a gate's existing hook; then advance the feature run, if any.

    The wrapped hook runs first, unchanged (for ``pr`` that is the changeset
    attestation — its outcome and any exception behave exactly as before).
    Only a request opened for a workflow run (``subject_type='workflow_run'``
    with a ``workflow_run_id``) submits an event; every other gate resolves
    exactly as it did before this hook existed.
    """

    gate_type: ClassVar[GateType]

    def __init__(
        self,
        *,
        wrapped: GateResolutionHook | None = None,
        engine_factory: EngineFactory | None = None,
    ) -> None:
        self._wrapped = wrapped
        self._engine_factory = engine_factory or _default_engine_factory

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
        event = event_for_resolution(request.gate_type, request.status)
        if self._wrapped is not None:
            try:
                outcome = await self._wrapped.on_resolved(request, decision, actor, session=session)
            except Exception:
                if for_run:
                    # A failed pr attestation blocks the gate: the run never moves on it.
                    mark_blocked(request, event=event, actor_ref=actor.actor_ref)
                raise
        else:
            outcome = ResolutionOutcome(
                completed=True, follow_up_state=f"{request.gate_type.value}_{request.status.value}"
            )

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
            request.gate_payload = {
                **request.gate_payload,
                "workflow_event": {
                    "status": "failed",
                    "event": event,
                    "actor": actor.actor_ref,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            }
            outcome.details = {
                **outcome.details,
                "workflow_event": event,
                "workflow_event_error": f"{type(exc).__name__}: {exc}",
            }
            if self._wrapped is None:
                outcome.completed = False
                outcome.blocking_reasons = [
                    *outcome.blocking_reasons,
                    f"submitting workflow event '{event}' failed; "
                    "the decision is recorded and the run can be synced",
                ]
            return outcome

        submitted: dict[str, Any] = {
            "status": "submitted",
            "event": event,
            "actor": actor.actor_ref,
            "to_state": new_state,
        }
        if event == EVENT_CANCEL:
            submitted["reason"] = cancel_reason(request)
        request.gate_payload = {**request.gate_payload, "workflow_event": submitted}
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
    "WAITING_STATE_BY_GATE",
    "WORKFLOW_RUN_SUBJECT",
    "EngineFactory",
    "PlanWorkflowGateHook",
    "PrWorkflowGateHook",
    "SpecWorkflowGateHook",
    "agent_actor_for_run",
    "attested_or_blocked",
    "cancel_reason",
    "event_for_resolution",
    "mark_blocked",
    "submit_gate_event",
]
