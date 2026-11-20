"""A decision is written only while its request is still pending (#191).

An expiry or another decision that lands between this decision's read and its
write stands; this decision is refused and its gate hook (which may run a held
action) never runs.
"""

from __future__ import annotations

import uuid

import pytest
from conftest import ADMIN_ID, WS, make_principal

from scf_approval.audit import RecordingAuditSink
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


@pytest.mark.parametrize("decision", [ApprovalAction.REJECT, ApprovalAction.ESCALATE])
async def test_a_refused_vote_is_audited_beside_its_stored_row(
    service: ApprovalService,
    repo: InMemoryApprovalRepository,
    fake_audit: RecordingAuditSink,
    decision: ApprovalAction,
) -> None:
    """The vote row is written before the pending-only status write refuses it; the
    audit chain says so, so the decision table and the audit agree."""
    request = await service.create(
        workspace_id=WS,
        gate_type=GateType.AUTOMATION_ACTION,
        subject_type="automation_action",
        subject_id=uuid.uuid4(),
    )

    def approved_meanwhile(_: object) -> None:
        repo._items[request.id].status = GateStatus.APPROVED

    with pytest.raises(AlreadyResolvedError):
        await service.resolve(
            request.id,
            ApprovalDecisionRequest(decision=decision, note="not today"),
            make_principal(role=Role.ADMIN, principal_id=ADMIN_ID),
            workspace_id=WS,
            before_decision=approved_meanwhile,
        )
    [vote] = await service.decisions(request.id, workspace_id=WS)
    refused = [r for r in fake_audit.records if r["status"] == "decision_refused"]
    assert [(r["actor"], r["request_id"], r["metadata"]["decision"]) for r in refused] == [
        (f"user:{ADMIN_ID}", request.id, vote.decision.value)
    ]
    assert refused[0]["metadata"]["reason"] == "already_approved"
