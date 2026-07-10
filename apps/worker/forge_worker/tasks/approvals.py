"""F36 approval worker tasks (Celery beat + the grant-consumption contract).

Two DB-backed operations against the canonical F36 tables:

* :func:`run_expire_sweep` — the SLA sweeper: pending ``approval_request`` rows
  past ``expires_at`` flip to ``expired`` (a terminal state; the subject's
  workflow routes to ``needs_human_input`` downstream). One immutable
  ``approval.expired`` audit row is written per swept gate; a re-run is
  idempotent.
* :func:`run_consume_grant` — the *consumption* side of J5, implementing the
  frozen ``PolicyOverrideGate.consume`` contract for the agent-runtime resume
  path: atomically check-and-consume a non-expired, unconsumed
  ``policy_override_grant`` bound to the exact ``(agent_run_id,
  action_fingerprint)``. ``True`` allows exactly one call; any mismatch,
  expiry, or prior consumption denies. The single UPDATE-where-active
  statement makes double-consumption impossible even across workers.

Pure session-in functions (the authz-purge pattern) so tests run on hermetic
SQLite; the Celery entrypoints open a session from the default factory.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session, sessionmaker

from forge_approval.delegation import DelegationDirectory
from forge_approval.escalation import EscalationOutcome, SlaPolicy, route_escalation
from forge_approval.models import ApprovalRequest as ApprovalRequestDTO
from forge_approval.models import GateType
from forge_db.models import ApprovalRequest, ApprovalStatus, AuditLog, PolicyOverrideGrant
from forge_db.session import create_session_factory
from forge_worker.celery_app import celery_app

__all__ = [
    "CONSUME_OVERRIDE_GRANT_TASK",
    "EXPIRE_APPROVALS_TASK",
    "SlaSweepResult",
    "consume_override_grant",
    "expire_pending_approvals",
    "run_consume_grant",
    "run_expire_sweep",
    "run_sla_sweep",
]

EXPIRE_APPROVALS_TASK = "approvals.expire_pending"
CONSUME_OVERRIDE_GRANT_TASK = "approvals.consume_override_grant"

#: Default fraction of a gate's SLA window after which it escalates (before the
#: hard expiry deadline). Tunable via ``FORGE_APPROVAL_ESCALATE_RATIO``.
_DEFAULT_ESCALATE_RATIO = 0.75


def run_expire_sweep(session: Session, *, now: datetime | None = None) -> int:
    """Mark overdue pending gates ``expired``; returns the number swept.

    Idempotent: swept gates are no longer ``pending``, so a second run finds
    nothing and writes no further audit rows.
    """
    now = now or datetime.now(UTC)
    overdue = session.scalars(
        select(ApprovalRequest).where(
            ApprovalRequest.status == ApprovalStatus.PENDING,
            ApprovalRequest.expires_at.is_not(None),
            ApprovalRequest.expires_at < now,
        )
    ).all()
    for request in overdue:
        request.status = ApprovalStatus.EXPIRED
        request.decided_at = now
        session.add(
            AuditLog(
                workspace_id=request.workspace_id,
                action="approval.expired",
                actor_type="system",
                target_type="approval_request",
                target_id=request.id,
                details={
                    "gate": request.gate.value,
                    "expired_at": now.isoformat(),
                    "follow_up_state": "needs_human_input",
                },
            )
        )
    session.commit()
    return len(overdue)


@dataclass
class SlaSweepResult:
    """Counts of the actions the SLA sweep applied to aging pending gates."""

    expired: int = 0
    escalated: int = 0
    routed: int = 0

    @property
    def total(self) -> int:
        return self.expired + self.escalated + self.routed


def _aware(moment: datetime | None) -> datetime | None:
    """Coerce a possibly-naive timestamp to UTC-aware (SQLite stores naive)."""
    if moment is None:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _sla_for(request: ApprovalRequest, *, escalate_ratio: float) -> SlaPolicy | None:
    """Derive a per-gate :class:`SlaPolicy` from its ``created_at``/``expires_at``.

    A gate with no ``expires_at`` carries no SLA (returns ``None`` — it is left
    pending, matching the historical sweeper). Otherwise the escalation threshold
    sits at ``escalate_ratio`` of the window before the hard expiry deadline.
    """
    created = _aware(request.created_at)
    expires = _aware(request.expires_at)
    if expires is None or created is None:
        return None
    window = (expires - created).total_seconds()
    expire_after = max(0, int(window))
    escalate_after = max(0, int(window * escalate_ratio))
    return SlaPolicy(escalate_after_seconds=escalate_after, expire_after_seconds=expire_after)


def _assignee_of(request: ApprovalRequest) -> uuid.UUID | None:
    raw = (request.payload or {}).get("assignee")
    if not isinstance(raw, str):
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


def run_sla_sweep(
    session: Session,
    *,
    now: datetime | None = None,
    delegation: DelegationDirectory | None = None,
    escalate_ratio: float = _DEFAULT_ESCALATE_RATIO,
) -> SlaSweepResult:
    """Route / escalate / expire aging pending gates (the SLA ladder).

    Extends the bare expiry sweep: before a gate's hard deadline it is *routed*
    (an OOO assignee handed to their delegate, per ``delegation``) or *escalated*
    (its resolving bar raised to admin, ``escalated=True`` + critical risk); only
    at the deadline is it expired. Each action writes one immutable audit row and
    is idempotent (an escalated gate re-escalated is a no-op; an expired gate
    leaves the pending set). Returns the per-action counts.
    """
    now = _aware(now) or datetime.now(UTC)
    result = SlaSweepResult()
    pending = session.scalars(
        select(ApprovalRequest).where(ApprovalRequest.status == ApprovalStatus.PENDING)
    ).all()
    for request in pending:
        sla = _sla_for(request, escalate_ratio=escalate_ratio)
        if sla is None:
            continue
        dto = ApprovalRequestDTO(
            id=request.id,
            workspace_id=request.workspace_id,
            gate_type=GateType(request.gate.value),
            requested_at=_aware(request.created_at),
            escalated=request.escalated,
        )
        decision = route_escalation(
            dto, now=now, sla=sla, delegation=delegation, assignee=_assignee_of(request)
        )
        if decision.outcome is EscalationOutcome.EXPIRE:
            _expire_gate(session, request, now)
            result.expired += 1
        elif decision.outcome is EscalationOutcome.ESCALATE_TO_ADMIN:
            if not request.escalated:
                _escalate_gate(session, request, now, decision.reason)
                result.escalated += 1
        elif decision.outcome is EscalationOutcome.ROUTE_TO_DELEGATE and decision.route_to:
            _route_gate(session, request, now, decision.route_to, decision.reason)
            result.routed += 1
    session.commit()
    return result


def _expire_gate(session: Session, request: ApprovalRequest, now: datetime) -> None:
    request.status = ApprovalStatus.EXPIRED
    request.decided_at = now
    session.add(
        AuditLog(
            workspace_id=request.workspace_id,
            action="approval.expired",
            actor_type="system",
            target_type="approval_request",
            target_id=request.id,
            details={
                "gate": request.gate.value,
                "expired_at": now.isoformat(),
                "follow_up_state": "needs_human_input",
            },
        )
    )


def _escalate_gate(session: Session, request: ApprovalRequest, now: datetime, reason: str) -> None:
    request.escalated = True
    request.risk_level = "critical"
    session.add(
        AuditLog(
            workspace_id=request.workspace_id,
            action="approval.escalated",
            actor_type="system",
            target_type="approval_request",
            target_id=request.id,
            details={"gate": request.gate.value, "reason": reason, "at": now.isoformat()},
        )
    )


def _route_gate(
    session: Session,
    request: ApprovalRequest,
    now: datetime,
    delegate: uuid.UUID,
    reason: str,
) -> None:
    request.payload = {**(request.payload or {}), "assignee": str(delegate)}
    session.add(
        AuditLog(
            workspace_id=request.workspace_id,
            action="approval.routed_to_delegate",
            actor_type="system",
            target_type="approval_request",
            target_id=request.id,
            details={"gate": request.gate.value, "delegate": str(delegate), "reason": reason},
        )
    )


def run_consume_grant(
    session: Session,
    *,
    agent_run_id: uuid.UUID,
    action_fingerprint: str,
    now: datetime | None = None,
) -> bool:
    """Atomically consume the active grant for this exact action, if any.

    Returns ``True`` (allow this single call) only when a matching,
    unconsumed, unexpired grant existed and was flipped by THIS statement —
    never granting future scope (Build-Prompt constraint #2).
    """
    now = now or datetime.now(UTC)
    result = cast(
        "CursorResult[Any]",
        session.execute(
            update(PolicyOverrideGrant)
            .where(
                PolicyOverrideGrant.agent_run_id == agent_run_id,
                PolicyOverrideGrant.action_fingerprint == action_fingerprint,
                PolicyOverrideGrant.consumed.is_(False),
                PolicyOverrideGrant.expires_at > now,
            )
            .values(consumed=True)
        ),
    )
    consumed = (result.rowcount or 0) > 0
    if consumed:
        grant = session.scalars(
            select(PolicyOverrideGrant).where(
                PolicyOverrideGrant.agent_run_id == agent_run_id,
                PolicyOverrideGrant.action_fingerprint == action_fingerprint,
                PolicyOverrideGrant.consumed.is_(True),
            )
        ).first()
        if grant is not None:
            session.add(
                AuditLog(
                    workspace_id=grant.workspace_id,
                    action="policy_override.consumed",
                    actor_type="system",
                    target_type="policy_override_grant",
                    target_id=grant.id,
                    details={"agent_run_id": str(agent_run_id)},
                )
            )
    session.commit()
    return consumed


def _escalate_ratio() -> float:
    """Escalation threshold fraction from ``FORGE_APPROVAL_ESCALATE_RATIO``."""
    try:
        return float(os.environ.get("FORGE_APPROVAL_ESCALATE_RATIO", _DEFAULT_ESCALATE_RATIO))
    except ValueError:
        return _DEFAULT_ESCALATE_RATIO


@celery_app.task(name=EXPIRE_APPROVALS_TASK)
def expire_pending_approvals() -> int:
    """Beat entrypoint: sweep the SLA ladder (route -> escalate -> expire).

    Returns the total number of gates actioned this pass. Delegation routing is
    driven by any workspace OOO directory; absent one, gates escalate to admin
    before expiring at the hard deadline.
    """
    factory: sessionmaker[Session] = create_session_factory()
    with factory() as session:
        return run_sla_sweep(session, escalate_ratio=_escalate_ratio()).total


@celery_app.task(name=CONSUME_OVERRIDE_GRANT_TASK)
def consume_override_grant(agent_run_id: str, action_fingerprint: str) -> bool:
    """Resume-path entrypoint: consume the single-use grant for one call."""
    factory: sessionmaker[Session] = create_session_factory()
    with factory() as session:
        return run_consume_grant(
            session,
            agent_run_id=uuid.UUID(agent_run_id),
            action_fingerprint=action_fingerprint,
        )
