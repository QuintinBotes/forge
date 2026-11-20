"""A reason is optional on every decision and recorded when given (#191, FR-028).

Owner decision of 2026-11-15: never required, on any gate or decision kind,
including a decision on a held action and a ``reject``.
"""

from __future__ import annotations

import uuid

import pytest
from conftest import ADMIN_ID, WS, make_principal

from scf_approval.models import (
    ApprovalAction,
    ApprovalDecisionRequest,
    GateStatus,
    GateType,
    Role,
)
from scf_approval.repository import AlreadyResolvedError, InMemoryApprovalRepository
from scf_approval.service import ApprovalService

ADMIN = make_principal(role=Role.ADMIN, principal_id=ADMIN_ID)


async def _open(service: ApprovalService, gate_type: GateType) -> uuid.UUID:
    request = await service.create(
        workspace_id=WS,
        gate_type=gate_type,
        subject_type="subject",
        subject_id=uuid.uuid4(),
        requested_actor="system",
    )
    return request.id


@pytest.mark.parametrize("decision", list(ApprovalAction))
@pytest.mark.parametrize("gate_type", list(GateType))
async def test_a_decision_without_a_reason_is_accepted(
    service: ApprovalService, gate_type: GateType, decision: ApprovalAction
) -> None:
    approval_id = await _open(service, gate_type)

    await service.resolve(
        approval_id, ApprovalDecisionRequest(decision=decision), ADMIN, workspace_id=WS
    )

    [record] = await service.decisions(approval_id, workspace_id=WS)
    assert record.decision is decision
    assert record.note is None


@pytest.mark.parametrize("decision", list(ApprovalAction))
@pytest.mark.parametrize("gate_type", list(GateType))
async def test_a_reason_is_stored_with_the_decision(
    service: ApprovalService, gate_type: GateType, decision: ApprovalAction
) -> None:
    approval_id = await _open(service, gate_type)
    reason = "the target host is the staging relay, agreed with ops"

    await service.resolve(
        approval_id,
        ApprovalDecisionRequest(decision=decision, note=reason),
        ADMIN,
        workspace_id=WS,
    )

    [record] = await service.decisions(approval_id, workspace_id=WS)
    assert record.note == reason
    request = await service.get(approval_id, workspace_id=WS)
    if request.status is not GateStatus.PENDING:
        assert request.decision_note == reason


async def test_a_decision_never_overwrites_an_outcome_recorded_meanwhile(
    service: ApprovalService, repo: InMemoryApprovalRepository
) -> None:
    """Another decision or an expiry lands between this decision's read and its write."""
    approval_id = await _open(service, GateType.AUTOMATION_ACTION)

    def expire_meanwhile(_: object) -> None:
        repo._items[approval_id].status = GateStatus.EXPIRED  # as a concurrent sweep would

    with pytest.raises(AlreadyResolvedError):
        await service.resolve(
            approval_id,
            ApprovalDecisionRequest(decision=ApprovalAction.APPROVE),
            ADMIN,
            workspace_id=WS,
            before_decision=expire_meanwhile,
        )
    assert (await service.get(approval_id, workspace_id=WS)).status is GateStatus.EXPIRED


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
async def test_a_blank_reason_is_recorded_as_no_reason(
    service: ApprovalService, blank: str
) -> None:
    approval_id = await _open(service, GateType.AUTOMATION_ACTION)

    await service.resolve(
        approval_id,
        ApprovalDecisionRequest(decision=ApprovalAction.REJECT, note=blank),
        ADMIN,
        workspace_id=WS,
    )

    [record] = await service.decisions(approval_id, workspace_id=WS)
    assert record.note is None
    assert (await service.get(approval_id, workspace_id=WS)).decision_note is None
