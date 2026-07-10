"""F40-POL-GOVERNANCE — repo ``review_rules.min_approvals`` drives the gate quorum.

Proves the quorum is *policy-driven*: a PR gate opened for a repo whose policy
raises ``min_approvals`` becomes a multi-approver gate even when the caller passed
the default ``required_approvals=1`` — closing the "raising min_approvals never
creates a quorum gate" gap.
"""

from __future__ import annotations

import uuid

from conftest import WS

from forge_approval import ApprovalService
from forge_approval.models import (
    ApprovalAction,
    ApprovalDecisionRequest,
    GateStatus,
    GateType,
    Principal,
    Role,
)

MEMBER_A = uuid.UUID("00000000-0000-0000-0000-0000000000e1")
MEMBER_B = uuid.UUID("00000000-0000-0000-0000-0000000000e2")


def _member(uid: uuid.UUID) -> Principal:
    return Principal(kind="user", id=uid, role=Role.MEMBER, workspace_id=WS)


async def test_policy_min_approvals_raises_quorum(service: ApprovalService) -> None:
    gate = await service.create(
        workspace_id=WS,
        gate_type=GateType.PR,
        subject_type="workflow_run",
        subject_id=uuid.uuid4(),
        required_approvals=1,  # caller default — policy must still win
        gate_payload={"review_rules": {"min_approvals": 2}},
        requested_actor="system",
    )
    assert gate.required_approvals == 2

    approve = ApprovalDecisionRequest(decision=ApprovalAction.APPROVE)
    first = await service.resolve(gate.id, approve, _member(MEMBER_A), workspace_id=WS)
    assert first.status is GateStatus.PENDING

    second = await service.resolve(gate.id, approve, _member(MEMBER_B), workspace_id=WS)
    assert second.status is GateStatus.APPROVED


async def test_caller_quorum_is_not_lowered_by_policy(service: ApprovalService) -> None:
    # A caller asking for 3 is never lowered to the policy's 2 (max wins).
    gate = await service.create(
        workspace_id=WS,
        gate_type=GateType.PR,
        subject_type="workflow_run",
        subject_id=uuid.uuid4(),
        required_approvals=3,
        gate_payload={"review_rules": {"min_approvals": 2}},
        requested_actor="system",
    )
    assert gate.required_approvals == 3


async def test_non_pr_gate_ignores_review_rules_quorum(service: ApprovalService) -> None:
    gate = await service.create(
        workspace_id=WS,
        gate_type=GateType.DEPLOY,
        subject_type="workflow_run",
        subject_id=uuid.uuid4(),
        required_approvals=1,
        gate_payload={"review_rules": {"min_approvals": 5}},
        requested_actor="system",
    )
    assert gate.required_approvals == 1
