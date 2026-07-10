"""SLA-driven escalation *routing* for pending approval gates (F40).

Previously the only time-based action on a pending gate was the SLA sweeper, which
*expires* an overdue gate (``approvals.run_expire_sweep``). Expiry is the last
resort — before it, an aging gate should be **routed**: to a standing OOO delegate
if the assigned approver is out of office, otherwise escalated (its resolving bar
raised to admin). This module computes that routing decision as a pure function of
the request, an SLA policy, the clock, and the OOO :class:`DelegationDirectory`.

The decision is advisory data (no I/O): the beat/service applies it (re-assign,
set ``escalated=True``, or expire) and records the audit row. Keeping the policy
pure keeps it total and unit-testable without a database.
"""

from __future__ import annotations

import enum
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from forge_approval.delegation import DelegationDirectory
from forge_approval.models import ApprovalRequest, Role

__all__ = [
    "EscalationDecision",
    "EscalationOutcome",
    "SlaPolicy",
    "route_escalation",
]


class EscalationOutcome(enum.StrEnum):
    """What should happen to an aging gate."""

    NONE = "none"  # within SLA — leave it pending
    ROUTE_TO_DELEGATE = "route_to_delegate"  # assignee OOO — hand to their delegate
    ESCALATE_TO_ADMIN = "escalate_to_admin"  # past escalation SLA — raise the bar
    EXPIRE = "expire"  # past the hard deadline — terminal


class SlaPolicy(BaseModel):
    """Time thresholds governing a gate's escalation ladder."""

    escalate_after_seconds: int | None = None
    expire_after_seconds: int | None = None
    escalate_to: Role = Role.ADMIN


class EscalationDecision(BaseModel):
    """The routing action for one aging gate."""

    outcome: EscalationOutcome = EscalationOutcome.NONE
    route_to: UUID | None = None
    escalate: bool = False
    new_required_role: Role | None = None
    reason: str = ""
    aged_seconds: int = 0

    @property
    def is_actionable(self) -> bool:
        return self.outcome is not EscalationOutcome.NONE


def _age_seconds(request: ApprovalRequest, now: datetime) -> int:
    requested_at = request.requested_at
    if requested_at is None:
        return 0
    return max(0, int((now - requested_at).total_seconds()))


def route_escalation(
    request: ApprovalRequest,
    *,
    now: datetime,
    sla: SlaPolicy,
    delegation: DelegationDirectory | None = None,
    assignee: UUID | None = None,
) -> EscalationDecision:
    """Compute the escalation routing for one pending gate.

    Ladder (first hit wins): hard-expiry deadline -> OOO delegate route ->
    escalate-to-admin -> leave pending. An assignee who is OOO (per
    ``delegation``) is routed to their delegate the moment the escalation SLA is
    crossed, ahead of the admin escalation.
    """
    aged = _age_seconds(request, now)

    if sla.expire_after_seconds is not None and aged >= sla.expire_after_seconds:
        return EscalationDecision(
            outcome=EscalationOutcome.EXPIRE,
            reason=f"gate exceeded expiry SLA ({sla.expire_after_seconds}s)",
            aged_seconds=aged,
        )

    if sla.escalate_after_seconds is not None and aged >= sla.escalate_after_seconds:
        if delegation is not None and assignee is not None:
            delegate = delegation.resolve(assignee, now)
            if delegate != assignee:
                return EscalationDecision(
                    outcome=EscalationOutcome.ROUTE_TO_DELEGATE,
                    route_to=delegate,
                    reason=f"assignee {assignee} is out of office; routed to delegate",
                    aged_seconds=aged,
                )
        return EscalationDecision(
            outcome=EscalationOutcome.ESCALATE_TO_ADMIN,
            escalate=True,
            new_required_role=sla.escalate_to,
            reason=f"gate exceeded escalation SLA ({sla.escalate_after_seconds}s)",
            aged_seconds=aged,
        )

    return EscalationDecision(outcome=EscalationOutcome.NONE, aged_seconds=aged)
