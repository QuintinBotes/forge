"""A decision is written only while its request is still pending (#191).

An expiry or another decision that lands between this decision's read and its
write stands; this decision is refused and its gate hook (which may run a held
action) never runs.
"""

from __future__ import annotations

import uuid

import pytest
from conftest import ADMIN_ID, WS, make_principal

from scf_approval.models import ApprovalAction, ApprovalDecisionRequest, GateStatus, GateType, Role
from scf_approval.repository import AlreadyResolvedError, InMemoryApprovalRepository
from scf_approval.service import ApprovalService


async def test_a_decision_never_overwrites_an_outcome_recorded_meanwhile(
    service: ApprovalService, repo: InMemoryApprovalRepository
) -> None:
    request = await service.create(
        workspace_id=WS,
        gate_type=GateType.AUTOMATION_ACTION,
        subject_type="automation_action",
        subject_id=uuid.uuid4(),
    )

    def expire_meanwhile(_: object) -> None:
        repo._items[request.id].status = GateStatus.EXPIRED  # as a concurrent sweep would

    with pytest.raises(AlreadyResolvedError):
        await service.resolve(
            request.id,
            ApprovalDecisionRequest(decision=ApprovalAction.APPROVE),
            make_principal(role=Role.ADMIN, principal_id=ADMIN_ID),
            workspace_id=WS,
            before_decision=expire_meanwhile,
        )
    assert (await service.get(request.id, workspace_id=WS)).status is GateStatus.EXPIRED
