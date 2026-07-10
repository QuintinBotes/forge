"""F40-POL-GOVERNANCE — SLA routing/escalation wired into ``ApprovalService``.

Proves the SLA ladder is *applied*, not just computable: aging pending gates are
routed to an OOO delegate, escalated to admin, or expired — the enforcement the
bare expiry sweep never did.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from conftest import WS

from forge_approval import ApprovalService
from forge_approval.delegation import DelegationDirectory, DelegationEntry
from forge_approval.escalation import EscalationOutcome, SlaPolicy
from forge_approval.models import GateStatus, GateType

ASSIGNEE = uuid.UUID("00000000-0000-0000-0000-00000000a001")
DELEGATE = uuid.UUID("00000000-0000-0000-0000-00000000a002")

_SLA = SlaPolicy(escalate_after_seconds=10, expire_after_seconds=100)


async def _aged_gate(service: ApprovalService, *, seconds_old: int, payload: dict | None = None):
    gate = await service.create(
        workspace_id=WS,
        gate_type=GateType.PR,
        subject_type="workflow_run",
        subject_id=uuid.uuid4(),
        gate_payload=payload or {},
        requested_actor="system",
    )
    # Backdate the stored request so the SLA ladder sees an aged gate.
    stored = service._repo._items[gate.id]  # type: ignore[attr-defined]
    stored.requested_at = datetime.now(UTC) - timedelta(seconds=seconds_old)
    return gate


async def test_over_sla_gate_escalates_to_admin(service: ApprovalService) -> None:
    gate = await _aged_gate(service, seconds_old=50)
    decisions = await service.sweep_sla(sla=_SLA)
    assert [d.outcome for d in decisions] == [EscalationOutcome.ESCALATE_TO_ADMIN]

    refreshed = await service.get(gate.id, workspace_id=WS)
    assert refreshed.escalated is True
    assert refreshed.risk_level == "critical"
    assert refreshed.status is GateStatus.PENDING


async def test_ooo_assignee_routes_to_delegate(service: ApprovalService) -> None:
    gate = await _aged_gate(service, seconds_old=50, payload={"assignee": str(ASSIGNEE)})
    directory = DelegationDirectory(
        entries=[DelegationEntry(user_id=ASSIGNEE, delegate_id=DELEGATE)]
    )
    decisions = await service.sweep_sla(sla=_SLA, delegation=directory)
    assert [d.outcome for d in decisions] == [EscalationOutcome.ROUTE_TO_DELEGATE]

    refreshed = await service.get(gate.id, workspace_id=WS)
    assert refreshed.gate_payload["assignee"] == str(DELEGATE)
    assert refreshed.status is GateStatus.PENDING
    assert refreshed.escalated is False


async def test_past_deadline_gate_expires(service: ApprovalService) -> None:
    gate = await _aged_gate(service, seconds_old=200)
    decisions = await service.sweep_sla(sla=_SLA)
    assert [d.outcome for d in decisions] == [EscalationOutcome.EXPIRE]

    refreshed = await service.get(gate.id, workspace_id=WS)
    assert refreshed.status is GateStatus.EXPIRED


async def test_within_sla_gate_untouched(service: ApprovalService) -> None:
    gate = await _aged_gate(service, seconds_old=5)
    assert await service.sweep_sla(sla=_SLA) == []
    refreshed = await service.get(gate.id, workspace_id=WS)
    assert refreshed.status is GateStatus.PENDING
    assert refreshed.escalated is False


async def test_escalation_is_idempotent(service: ApprovalService) -> None:
    await _aged_gate(service, seconds_old=50)
    assert len(await service.sweep_sla(sla=_SLA)) == 1
    assert await service.sweep_sla(sla=_SLA) == []  # already escalated — no-op
