"""F40-POL-GOVERNANCE — the ``min_approvals > 1`` multi-approver quorum gate."""

from __future__ import annotations

import uuid

from conftest import WS

from forge_approval import ApprovalService, quorum_met
from forge_approval.models import (
    ApprovalAction,
    ApprovalDecisionRequest,
    GateStatus,
    GateType,
    Principal,
    Role,
)

MEMBER_A = uuid.UUID("00000000-0000-0000-0000-0000000000f1")
MEMBER_B = uuid.UUID("00000000-0000-0000-0000-0000000000f2")


def _member(uid: uuid.UUID) -> Principal:
    return Principal(kind="user", id=uid, role=Role.MEMBER, workspace_id=WS)


def test_quorum_met_counts_distinct_approvers() -> None:
    assert quorum_met([MEMBER_A], 2) is False
    assert quorum_met([MEMBER_A, MEMBER_A], 2) is False  # same approver twice
    assert quorum_met([MEMBER_A, MEMBER_B], 2) is True
    assert quorum_met([], 1) is False


async def test_two_approvals_required_holds_pending_until_quorum(
    service: ApprovalService,
) -> None:
    request = await service.create(
        workspace_id=WS,
        gate_type=GateType.PR,
        subject_type="workflow_run",
        subject_id=uuid.uuid4(),
        required_approvals=2,
        requested_actor="system",
    )
    approve = ApprovalDecisionRequest(decision=ApprovalAction.APPROVE)

    first = await service.resolve(request.id, approve, _member(MEMBER_A), workspace_id=WS)
    assert first.status is GateStatus.PENDING
    assert first.outcome.follow_up_state == "awaiting_more_approvals"
    assert first.outcome.details["approvals"] == 1

    second = await service.resolve(request.id, approve, _member(MEMBER_B), workspace_id=WS)
    assert second.status is GateStatus.APPROVED
    assert second.outcome.completed is True


async def test_single_approval_gate_resolves_immediately(service: ApprovalService) -> None:
    request = await service.create(
        workspace_id=WS,
        gate_type=GateType.PR,
        subject_type="workflow_run",
        subject_id=uuid.uuid4(),
        required_approvals=1,
        requested_actor="system",
    )
    resolution = await service.resolve(
        request.id,
        ApprovalDecisionRequest(decision=ApprovalAction.APPROVE),
        _member(MEMBER_A),
        workspace_id=WS,
    )
    assert resolution.status is GateStatus.APPROVED
