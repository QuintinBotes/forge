"""F40-POL-GOVERNANCE — SLA escalation *routing* (not just expiry)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from forge_approval import (
    DelegationDirectory,
    DelegationEntry,
    EscalationOutcome,
    Role,
    SlaPolicy,
    route_escalation,
)
from forge_approval.models import ApprovalRequest, GateType

ALICE = uuid.UUID("00000000-0000-0000-0000-0000000000e1")
BOB = uuid.UUID("00000000-0000-0000-0000-0000000000e2")
WS = uuid.UUID("00000000-0000-0000-0000-0000000000ef")


def _request(age_seconds: int) -> ApprovalRequest:
    now = datetime.now(UTC)
    return ApprovalRequest(
        id=uuid.uuid4(),
        workspace_id=WS,
        gate_type=GateType.PR,
        requested_at=now - timedelta(seconds=age_seconds),
    )


NOW = datetime.now(UTC)


def test_within_sla_is_no_action() -> None:
    sla = SlaPolicy(escalate_after_seconds=3600, expire_after_seconds=7200)
    decision = route_escalation(_request(60), now=NOW, sla=sla)
    assert decision.outcome is EscalationOutcome.NONE
    assert not decision.is_actionable


def test_past_escalation_sla_escalates_to_admin() -> None:
    sla = SlaPolicy(escalate_after_seconds=3600, expire_after_seconds=7200)
    decision = route_escalation(_request(3700), now=NOW, sla=sla)
    assert decision.outcome is EscalationOutcome.ESCALATE_TO_ADMIN
    assert decision.escalate is True
    assert decision.new_required_role is Role.ADMIN


def test_past_expiry_sla_expires() -> None:
    sla = SlaPolicy(escalate_after_seconds=3600, expire_after_seconds=7200)
    decision = route_escalation(_request(7300), now=NOW, sla=sla)
    assert decision.outcome is EscalationOutcome.EXPIRE


def test_ooo_assignee_routes_to_delegate_before_admin() -> None:
    sla = SlaPolicy(escalate_after_seconds=3600, expire_after_seconds=7200)
    delegation = DelegationDirectory(entries=[DelegationEntry(user_id=ALICE, delegate_id=BOB)])
    decision = route_escalation(
        _request(3700), now=NOW, sla=sla, delegation=delegation, assignee=ALICE
    )
    assert decision.outcome is EscalationOutcome.ROUTE_TO_DELEGATE
    assert decision.route_to == BOB


def test_available_assignee_still_escalates() -> None:
    sla = SlaPolicy(escalate_after_seconds=3600, expire_after_seconds=7200)
    delegation = DelegationDirectory()  # ALICE is not OOO
    decision = route_escalation(
        _request(3700), now=NOW, sla=sla, delegation=delegation, assignee=ALICE
    )
    assert decision.outcome is EscalationOutcome.ESCALATE_TO_ADMIN
