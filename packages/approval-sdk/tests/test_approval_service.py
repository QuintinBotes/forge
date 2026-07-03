"""Unit tests — ApprovalService create/list/context/resolve (F36 AC#1-#4, #9-#13, #16-#18)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from conftest import (
    ADMIN_ID,
    OTHER_WS,
    WS,
    RecordingRedactor,
    ScriptedHook,
    make_principal,
)

from forge_approval.audit import RecordingAuditSink
from forge_approval.authorizer import ApprovalAuthorizer
from forge_approval.events import (
    APPROVAL_REQUESTED_TOPIC,
    APPROVAL_RESOLVED_TOPIC,
    ApprovalRequestedEvent,
    ApprovalResolvedEvent,
    InMemoryActivityBus,
)
from forge_approval.models import (
    ApprovalAction,
    ApprovalDecisionRequest,
    GateStatus,
    GateType,
    ResolutionOutcome,
    Role,
)
from forge_approval.registry import GateRegistry
from forge_approval.repository import (
    AlreadyResolvedError,
    ApprovalNotFoundError,
    DuplicateDecisionError,
    InMemoryApprovalRepository,
)
from forge_approval.service import ApprovalService

APPROVE = ApprovalDecisionRequest(decision=ApprovalAction.APPROVE)


async def _create(service: ApprovalService, gate_type: GateType = GateType.PR, **over):
    params = {
        "workspace_id": WS,
        "gate_type": gate_type,
        "subject_type": "workflow_run",
        "subject_id": uuid.uuid4(),
        "requested_actor": "agent:00000000-0000-0000-0000-0000000000c1",
        "title": f"{gate_type.value} gate",
    }
    params.update(over)
    return await service.create(**params)


# --------------------------------------------------------------------------- #
# create                                                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("gate_type", list(GateType))
async def test_create_all_gate_types_emits_event_and_redacts(
    service: ApprovalService,
    fake_bus: InMemoryActivityBus,
    fake_audit: RecordingAuditSink,
    fake_redactor: RecordingRedactor,
    gate_type: GateType,
) -> None:
    """AC#1 + #17: create persists, redacts the payload, audits, emits once."""
    created = await _create(
        service, gate_type, gate_payload={"api_secret": "sk-realkey123", "note": "x"}
    )
    assert created.gate_type is gate_type
    assert created.status is GateStatus.PENDING
    assert created.workspace_id == WS
    assert created.gate_payload["api_secret"] == "[REDACTED]"  # redactor applied
    assert fake_redactor.calls  # redaction ran before persist

    events = fake_bus.by_topic(APPROVAL_REQUESTED_TOPIC)
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, ApprovalRequestedEvent)
    assert event.approval_id == created.id
    assert event.gate_type is gate_type

    assert fake_audit.records[-1]["status"] == "requested"
    assert fake_audit.records[-1]["request_id"] == created.id


async def test_create_idempotent_on_pending_unique(
    service: ApprovalService, fake_bus: InMemoryActivityBus
) -> None:
    """AC#2: same (subject_type, subject_id, gate_type) while pending => same gate."""
    subject_id = uuid.uuid4()
    first = await _create(service, subject_id=subject_id)
    second = await _create(service, subject_id=subject_id)
    assert second.id == first.id
    assert len(fake_bus.by_topic(APPROVAL_REQUESTED_TOPIC)) == 1  # no second event


async def test_create_after_resolution_opens_new_gate(service: ApprovalService) -> None:
    subject_id = uuid.uuid4()
    first = await _create(service, subject_id=subject_id)
    await service.resolve(
        first.id, APPROVE, make_principal(role=Role.ADMIN, principal_id=ADMIN_ID),
        workspace_id=WS,
    )
    second = await _create(service, subject_id=subject_id)
    assert second.id != first.id


# --------------------------------------------------------------------------- #
# read / context                                                               #
# --------------------------------------------------------------------------- #


async def test_get_context_delegates_to_provider(service: ApprovalService) -> None:
    """AC#3: context comes from the registered provider for the gate type."""
    created = await _create(service, GateType.SPEC)
    context = await service.get_context(created.id, workspace_id=WS)
    assert context.approval_id == created.id
    assert context.goal == "goal for spec"


async def test_available_actions_per_gate(service: ApprovalService) -> None:
    """AC#4: escalate offered only for incident_remediation / policy_override."""
    for gate_type in (GateType.SPEC, GateType.PLAN, GateType.PR, GateType.DEPLOY):
        created = await _create(service, gate_type)
        context = await service.get_context(created.id, workspace_id=WS)
        assert ApprovalAction.ESCALATE not in context.available_actions
        assert ApprovalAction.APPROVE in context.available_actions
    for gate_type in (GateType.INCIDENT_REMEDIATION, GateType.POLICY_OVERRIDE):
        created = await _create(service, gate_type)
        context = await service.get_context(created.id, workspace_id=WS)
        assert ApprovalAction.ESCALATE in context.available_actions


async def test_missing_provider_degrades_to_fallback_context(
    repo: InMemoryApprovalRepository,
    fake_bus: InMemoryActivityBus,
) -> None:
    """Slice risk #3: an unregistered gate is still shown read-only."""
    service = ApprovalService(repo, GateRegistry(), ApprovalAuthorizer(), events=fake_bus)
    created = await _create(service, GateType.INCIDENT_REMEDIATION)
    context = await service.get_context(created.id, workspace_id=WS)
    assert context.gate_type is GateType.INCIDENT_REMEDIATION
    assert ApprovalAction.ESCALATE in context.available_actions


async def test_tenant_isolation_get_and_context(service: ApprovalService) -> None:
    """AC#16: cross-workspace access looks like not-found, never forbidden."""
    created = await _create(service)
    with pytest.raises(ApprovalNotFoundError):
        await service.get(created.id, workspace_id=OTHER_WS)
    with pytest.raises(ApprovalNotFoundError):
        await service.get_context(created.id, workspace_id=OTHER_WS)


# --------------------------------------------------------------------------- #
# resolve                                                                      #
# --------------------------------------------------------------------------- #


async def test_resolve_persists_decision_sets_status_emits(
    service: ApprovalService,
    fake_bus: InMemoryActivityBus,
    fake_audit: RecordingAuditSink,
) -> None:
    """AC#9: one decision row, status=approved, resolved_at, one event."""
    created = await _create(service)
    actor = make_principal(role=Role.ADMIN, principal_id=ADMIN_ID)
    resolution = await service.resolve(created.id, APPROVE, actor, workspace_id=WS)

    assert resolution.status is GateStatus.APPROVED
    stored = await service.get(created.id, workspace_id=WS)
    assert stored.status is GateStatus.APPROVED
    assert stored.resolved_at is not None
    assert stored.resolver_user_id == ADMIN_ID

    decisions = await service.decisions(created.id, workspace_id=WS)
    assert len(decisions) == 1
    assert decisions[0].approver_user_id == ADMIN_ID
    assert decisions[0].decision is ApprovalAction.APPROVE

    events = fake_bus.by_topic(APPROVAL_RESOLVED_TOPIC)
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, ApprovalResolvedEvent)
    assert event.status is GateStatus.APPROVED
    assert fake_audit.records[-1]["status"] == "approved"


async def test_resolve_invokes_hook_and_folds_outcome(
    service: ApprovalService, fake_registry: GateRegistry
) -> None:
    """AC#10: the pr hook runs and its outcome lands in the resolution."""
    created = await _create(service)
    resolution = await service.resolve(
        created.id, APPROVE, make_principal(role=Role.ADMIN, principal_id=ADMIN_ID),
        workspace_id=WS,
    )
    assert resolution.outcome.completed is True
    assert resolution.outcome.follow_up_state == "merged"
    hook = fake_registry.hook(GateType.PR)
    assert isinstance(hook, ScriptedHook)
    assert len(hook.calls) == 1


async def test_approve_but_blocked_keeps_reasons(
    repo: InMemoryApprovalRepository, fake_bus: InMemoryActivityBus
) -> None:
    """AC#11 (F08 regression lock): approved status + blocking_reasons, no advance."""
    registry = GateRegistry()
    registry.register_hook(
        ScriptedHook(
            GateType.PR,
            ResolutionOutcome(
                completed=False,
                blocking_reasons=["CI status is failure (1 of 3 checks)"],
            ),
        )
    )
    service = ApprovalService(repo, registry, ApprovalAuthorizer(), events=fake_bus)
    created = await _create(service)
    resolution = await service.resolve(
        created.id, APPROVE, make_principal(role=Role.ADMIN, principal_id=ADMIN_ID),
        workspace_id=WS,
    )
    assert resolution.status is GateStatus.APPROVED  # the approval IS recorded
    assert resolution.outcome.completed is False
    assert resolution.outcome.blocking_reasons == ["CI status is failure (1 of 3 checks)"]
    assert resolution.outcome.follow_up_state is None  # no review_approved advance


async def test_resolve_without_hook_returns_not_implemented(
    repo: InMemoryApprovalRepository,
) -> None:
    """Emit-only gates resolve gracefully with a not_implemented outcome."""
    service = ApprovalService(repo, GateRegistry(), ApprovalAuthorizer())
    created = await _create(service, GateType.SPEC)
    resolution = await service.resolve(
        created.id, APPROVE, make_principal(role=Role.ADMIN, principal_id=ADMIN_ID),
        workspace_id=WS,
    )
    assert resolution.status is GateStatus.APPROVED
    assert resolution.outcome.details["result"] == "not_implemented"


async def test_request_changes_and_reject_route(service: ApprovalService) -> None:
    """AC#12: request_changes => changes_requested; reject => rejected."""
    admin = make_principal(role=Role.ADMIN, principal_id=ADMIN_ID)

    changes = await _create(service, subject_id=uuid.uuid4())
    res1 = await service.resolve(
        changes.id,
        ApprovalDecisionRequest(decision=ApprovalAction.REQUEST_CHANGES, note="tighten tests"),
        admin,
        workspace_id=WS,
    )
    assert res1.status is GateStatus.CHANGES_REQUESTED
    stored = await service.get(changes.id, workspace_id=WS)
    assert stored.decision_note == "tighten tests"

    rejected = await _create(service, subject_id=uuid.uuid4())
    res2 = await service.resolve(
        rejected.id,
        ApprovalDecisionRequest(decision=ApprovalAction.REJECT),
        admin,
        workspace_id=WS,
    )
    assert res2.status is GateStatus.REJECTED


async def test_escalate_raises_role_and_risk(
    service: ApprovalService, fake_bus: InMemoryActivityBus
) -> None:
    """AC#13: escalate keeps pending, risk=>critical, then members are refused."""
    created = await _create(service, GateType.INCIDENT_REMEDIATION)
    member = make_principal()
    resolution = await service.resolve(
        created.id,
        ApprovalDecisionRequest(decision=ApprovalAction.ESCALATE),
        member,
        workspace_id=WS,
    )
    assert resolution.status is GateStatus.PENDING
    stored = await service.get(created.id, workspace_id=WS)
    assert stored.risk_level == "critical"
    assert stored.escalated is True
    assert fake_bus.by_topic(APPROVAL_RESOLVED_TOPIC) == []  # still pending

    from forge_approval.authorizer import AuthorizationError

    other_member = make_principal(principal_id=uuid.uuid4())
    with pytest.raises(AuthorizationError):
        await service.resolve(created.id, APPROVE, other_member, workspace_id=WS)
    # An admin can now resolve it.
    final = await service.resolve(
        created.id, APPROVE, make_principal(role=Role.ADMIN, principal_id=ADMIN_ID),
        workspace_id=WS,
    )
    assert final.status is GateStatus.APPROVED


async def test_resolve_already_resolved_raises(service: ApprovalService) -> None:
    created = await _create(service)
    admin = make_principal(role=Role.ADMIN, principal_id=ADMIN_ID)
    await service.resolve(created.id, APPROVE, admin, workspace_id=WS)
    with pytest.raises(AlreadyResolvedError):
        await service.resolve(
            created.id, APPROVE, make_principal(principal_id=uuid.uuid4()), workspace_id=WS
        )


async def test_duplicate_vote_raises(service: ApprovalService) -> None:
    # Escalate keeps the gate pending, so a second vote by the SAME approver is
    # the one V1 path that can hit the unique-per-approver constraint. An admin
    # is used because escalation raises the resolving bar to admin.
    created = await _create(service, GateType.INCIDENT_REMEDIATION)
    admin = make_principal(role=Role.ADMIN, principal_id=ADMIN_ID)
    await service.resolve(
        created.id,
        ApprovalDecisionRequest(decision=ApprovalAction.ESCALATE),
        admin,
        workspace_id=WS,
    )
    with pytest.raises(DuplicateDecisionError):
        await service.resolve(
            created.id,
            ApprovalDecisionRequest(decision=ApprovalAction.ESCALATE),
            admin,
            workspace_id=WS,
        )


async def test_resolve_cross_workspace_not_found(service: ApprovalService) -> None:
    """AC#16: resolving another workspace's gate looks nonexistent."""
    created = await _create(service)
    actor = make_principal(role=Role.ADMIN, principal_id=ADMIN_ID, workspace_id=OTHER_WS)
    with pytest.raises(ApprovalNotFoundError):
        await service.resolve(created.id, APPROVE, actor, workspace_id=OTHER_WS)


# --------------------------------------------------------------------------- #
# inbox / count / expiry                                                       #
# --------------------------------------------------------------------------- #


async def test_inbox_scoped_sorted_and_mine(service: ApprovalService) -> None:
    """AC#18: critical first; mine=True keeps only resolvable gates."""
    await _create(service, GateType.PR, risk_level="info", subject_id=uuid.uuid4())
    await _create(
        service, GateType.POLICY_OVERRIDE, risk_level="critical", subject_id=uuid.uuid4()
    )
    await _create(service, GateType.DEPLOY, risk_level="warning", subject_id=uuid.uuid4())

    admin = make_principal(role=Role.ADMIN, principal_id=ADMIN_ID)
    rows = await service.list(workspace_id=WS, actor=admin, status=GateStatus.PENDING)
    assert [r.risk_level for r in rows] == ["critical", "warning", "info"]

    member = make_principal()
    mine = await service.list(
        workspace_id=WS, actor=member, status=GateStatus.PENDING, mine=True
    )
    # policy_override is admin-only, so the member's inbox excludes it.
    assert {r.gate_type for r in mine} == {GateType.PR, GateType.DEPLOY}

    count = await service.count(workspace_id=WS, actor=member, mine=True)
    assert count == len(mine)  # AC#18: count matches the inbox

    other = await service.list(
        workspace_id=OTHER_WS,
        actor=make_principal(workspace_id=OTHER_WS),
        status=GateStatus.PENDING,
    )
    assert other == []  # AC#16: list never crosses workspaces


async def test_inbox_filters(service: ApprovalService) -> None:
    project = uuid.uuid4()
    await _create(service, GateType.PR, project_id=project, subject_id=uuid.uuid4())
    await _create(service, GateType.DEPLOY, subject_id=uuid.uuid4())
    admin = make_principal(role=Role.ADMIN, principal_id=ADMIN_ID)

    by_gate = await service.list(workspace_id=WS, actor=admin, gate_type=GateType.PR)
    assert {r.gate_type for r in by_gate} == {GateType.PR}

    by_project = await service.list(workspace_id=WS, actor=admin, project_id=project)
    assert len(by_project) == 1


async def test_expire_pending_marks_and_emits(
    service: ApprovalService, fake_bus: InMemoryActivityBus
) -> None:
    """SLA sweep: past-expiry pending gates flip to expired + emit resolved."""
    now = datetime.now(UTC)
    expired = await _create(
        service, subject_id=uuid.uuid4(), expires_at=now - timedelta(minutes=1)
    )
    alive = await _create(
        service, subject_id=uuid.uuid4(), expires_at=now + timedelta(hours=1)
    )
    resolutions = await service.expire_pending(now=now)
    assert [r.approval_id for r in resolutions] == [expired.id]
    assert (await service.get(expired.id, workspace_id=WS)).status is GateStatus.EXPIRED
    assert (await service.get(alive.id, workspace_id=WS)).status is GateStatus.PENDING
    resolved_events = fake_bus.by_topic(APPROVAL_RESOLVED_TOPIC)
    assert len(resolved_events) == 1
    event = resolved_events[0]
    assert isinstance(event, ApprovalResolvedEvent)
    assert event.status is GateStatus.EXPIRED
    assert event.outcome.follow_up_state == "needs_human_input"
