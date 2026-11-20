"""Earlier decisions on the same kind of held action (#191, FR-029/FR-030).

Precedents inform the person; they never decide. Only people's decisions on held
actions of the same type in the same workspace count, newest first and bounded.
"""

from __future__ import annotations

import uuid

from conftest import ADMIN_ID, MEMBER_ID, OTHER_WS, WS, make_principal

from scf_approval.models import (
    ApprovalAction,
    ApprovalDecisionRecord,
    ApprovalDecisionRequest,
    GateStatus,
    GateType,
    Principal,
    Role,
)
from scf_approval.repository import InMemoryApprovalRepository
from scf_approval.service import PRECEDENT_LIMIT, ApprovalService

ADMIN = make_principal(role=Role.ADMIN, principal_id=ADMIN_ID)
MEMBER = make_principal(role=Role.MEMBER, principal_id=MEMBER_ID)


async def _hold(
    service: ApprovalService,
    action_type: str = "webhook_post",
    *,
    workspace_id: uuid.UUID = WS,
    gate_type: GateType = GateType.AUTOMATION_ACTION,
) -> uuid.UUID:
    request = await service.create(
        workspace_id=workspace_id,
        gate_type=gate_type,
        subject_type="automation_action",
        subject_id=uuid.uuid4(),
        requested_actor="automation",
        gate_payload={"action_type": action_type},
    )
    return request.id


async def _decide(
    service: ApprovalService,
    approval_id: uuid.UUID,
    decision: ApprovalAction,
    note: str | None = None,
    *,
    actor: Principal = ADMIN,
    workspace_id: uuid.UUID = WS,
) -> None:
    await service.resolve(
        approval_id,
        ApprovalDecisionRequest(decision=decision, note=note),
        actor,
        workspace_id=workspace_id,
    )


async def test_precedents_are_this_workspaces_decisions_on_the_same_action_type(
    service: ApprovalService,
) -> None:
    first = await _hold(service)
    await _decide(service, first, ApprovalAction.APPROVE, "staging relay, agreed with ops")
    second = await _hold(service)
    await _decide(service, second, ApprovalAction.REJECT, actor=MEMBER)
    # Not precedents: another action type, another workspace, another gate.
    other_type = await _hold(service, "trigger_deploy")
    await _decide(service, other_type, ApprovalAction.APPROVE, "deploys are fine")
    foreign = await _hold(service, workspace_id=OTHER_WS)
    await _decide(
        service,
        foreign,
        ApprovalAction.APPROVE,
        "another tenant's reason",
        actor=make_principal(role=Role.ADMIN, principal_id=ADMIN_ID, workspace_id=OTHER_WS),
        workspace_id=OTHER_WS,
    )
    other_gate = await _hold(service, gate_type=GateType.DEPLOY)
    await _decide(service, other_gate, ApprovalAction.APPROVE, "not a held action")

    precedents = await service.precedents(workspace_id=WS, action_type="webhook_post")

    assert [(p.approval_id, p.decision, p.decider_id, p.reason) for p in precedents] == [
        (second, ApprovalAction.REJECT, MEMBER_ID, None),
        (first, ApprovalAction.APPROVE, ADMIN_ID, "staging relay, agreed with ops"),
    ]
    assert all(p.action_type == "webhook_post" for p in precedents)
    assert all(p.decided_at is not None for p in precedents)
    decisions = await service.decisions(first, workspace_id=WS)
    assert precedents[1].decision_id == decisions[0].id


async def test_precedents_are_newest_first_and_bounded(service: ApprovalService) -> None:
    decided = []
    for n in range(PRECEDENT_LIMIT + 3):
        approval_id = await _hold(service)
        await _decide(service, approval_id, ApprovalAction.APPROVE, f"reason {n}")
        decided.append(approval_id)

    precedents = await service.precedents(workspace_id=WS, action_type="webhook_post")
    assert [p.approval_id for p in precedents] == decided[::-1][:PRECEDENT_LIMIT]

    two = await service.precedents(workspace_id=WS, action_type="webhook_post", limit=2)
    assert [p.approval_id for p in two] == decided[::-1][:2]


async def test_a_blank_note_stored_earlier_is_no_reason(
    service: ApprovalService, repo: InMemoryApprovalRepository
) -> None:
    approval_id = await _hold(service)
    # Written straight to storage, as a decision recorded before notes were normalised.
    await repo.add_decision(
        ApprovalDecisionRecord(
            approval_request_id=approval_id,
            approver_user_id=ADMIN_ID,
            decision=ApprovalAction.APPROVE,
            note="   ",
        )
    )
    [precedent] = await service.precedents(workspace_id=WS, action_type="webhook_post")
    assert precedent.reason is None


async def test_an_expired_hold_is_no_precedent(service: ApprovalService) -> None:
    approval_id = await _hold(service)
    await service.expire(await service.get(approval_id, workspace_id=WS), actor="system")
    assert await service.precedents(workspace_id=WS, action_type="webhook_post") == []


async def test_the_holds_context_shows_the_earlier_decisions(service: ApprovalService) -> None:
    earlier = await _hold(service)
    await _decide(service, earlier, ApprovalAction.APPROVE, "agreed with ops")
    current = await _hold(service)
    # The current hold's own partial votes are not "earlier" decisions.
    await _decide(service, current, ApprovalAction.ESCALATE, "needs an admin")

    context = await service.get_context(current, workspace_id=WS)

    assert context.precedents is not None
    assert [(p.approval_id, p.reason) for p in context.precedents] == [(earlier, "agreed with ops")]


async def test_another_gates_context_carries_no_precedents(service: ApprovalService) -> None:
    approval_id = await _hold(service, gate_type=GateType.DEPLOY)
    context = await service.get_context(approval_id, workspace_id=WS)
    assert context.precedents is None


async def test_any_number_of_approving_precedents_never_resolves_a_new_hold(
    service: ApprovalService,
) -> None:
    for _ in range(5):
        approval_id = await _hold(service)
        await _decide(service, approval_id, ApprovalAction.APPROVE, "fine every time")

    new = await _hold(service)

    request = await service.get(new, workspace_id=WS)
    assert request.status is GateStatus.PENDING
    assert await service.decisions(new, workspace_id=WS) == []
    context = await service.get_context(new, workspace_id=WS)
    assert len(context.precedents or []) == 5
    assert (await service.get(new, workspace_id=WS)).status is GateStatus.PENDING
