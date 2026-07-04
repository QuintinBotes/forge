"""Integration-style flow tests (F36 §7 — create → notify → authorize →
resolve → hook) for a ``pr`` and a ``policy_override`` gate, with the real
service + registry + F36-owned providers and hermetic fakes at the ports.
"""

from __future__ import annotations

import uuid

from conftest import ADMIN_ID, WS, ScriptedHook, make_principal

from forge_approval.audit import RecordingAuditSink
from forge_approval.authorizer import ApprovalAuthorizer
from forge_approval.events import (
    APPROVAL_REQUESTED_TOPIC,
    APPROVAL_RESOLVED_TOPIC,
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
from forge_approval.providers.deploy import DeployGateProvider, DeployResolutionHook
from forge_approval.providers.policy_override import (
    InMemoryGrantStore,
    PolicyOverrideGateProvider,
    PolicyOverrideResolutionHook,
    action_fingerprint,
)
from forge_approval.registry import GateRegistry
from forge_approval.repository import InMemoryApprovalRepository
from forge_approval.service import ApprovalService

ADMIN = make_principal(role=Role.ADMIN, principal_id=ADMIN_ID)
APPROVE = ApprovalDecisionRequest(decision=ApprovalAction.APPROVE)


def build_stack() -> tuple[
    ApprovalService, InMemoryGrantStore, InMemoryActivityBus, RecordingAuditSink
]:
    """The same composition the apps' composition root performs."""
    registry = GateRegistry()
    grants = InMemoryGrantStore()
    registry.register_provider(DeployGateProvider())
    registry.register_hook(DeployResolutionHook())
    registry.register_provider(PolicyOverrideGateProvider())
    registry.register_hook(PolicyOverrideResolutionHook(grants))
    registry.register_hook(
        ScriptedHook(
            GateType.PR,
            ResolutionOutcome(completed=True, follow_up_state="merged", details={"merged": True}),
        )
    )
    bus = InMemoryActivityBus()
    audit = RecordingAuditSink()
    service = ApprovalService(
        InMemoryApprovalRepository(),
        registry,
        ApprovalAuthorizer(),
        events=bus,
        audit=audit,
    )
    return service, grants, bus, audit


async def test_pr_gate_create_to_merged() -> None:
    """AC#1, #9, #10, #17 — the full pr journey through the unified service."""
    service, _, bus, audit = build_stack()
    created = await service.create(
        workspace_id=WS,
        gate_type=GateType.PR,
        subject_type="workflow_run",
        subject_id=uuid.uuid4(),
        workflow_run_id=uuid.uuid4(),
        requested_actor="agent:00000000-0000-0000-0000-0000000000c1",
        title="PR for TASK-42",
    )
    assert len(bus.by_topic(APPROVAL_REQUESTED_TOPIC)) == 1

    resolution = await service.resolve(created.id, APPROVE, ADMIN, workspace_id=WS)
    assert resolution.status is GateStatus.APPROVED
    assert resolution.outcome.details == {"merged": True}
    assert len(bus.by_topic(APPROVAL_RESOLVED_TOPIC)) == 1
    assert [r["status"] for r in audit.records] == ["requested", "approved"]


async def test_policy_override_grant_consumed_once() -> None:
    """AC#14 — create override gate → admin approve → grant → consume once."""
    service, grants, bus, _ = build_stack()
    agent_run_id = uuid.uuid4()
    call = {"tool": "shell", "action": "restricted_action", "arguments": {"env": "prod"}}
    fingerprint = action_fingerprint(call)

    created = await service.create(
        workspace_id=WS,
        gate_type=GateType.POLICY_OVERRIDE,
        subject_type="agent_run",
        subject_id=agent_run_id,
        agent_run_id=agent_run_id,
        requested_actor=f"agent:{uuid.uuid4()}",
        risk_level="critical",
        gate_payload={
            "action": call,
            "blocked_by": ["restricted_actions"],
            "severity": "critical",
            "action_fingerprint": fingerprint,
        },
    )
    context = await service.get_context(created.id, workspace_id=WS)
    assert context.gate_payload["action_fingerprint"] == fingerprint

    resolution = await service.resolve(created.id, APPROVE, ADMIN, workspace_id=WS)
    assert resolution.outcome.completed is True

    # The paused tool call resumes exactly once (the F06/F29 resume path).
    assert await grants.consume(agent_run_id=agent_run_id, action_fingerprint=fingerprint)
    assert not await grants.consume(agent_run_id=agent_run_id, action_fingerprint=fingerprint)

    resolved = bus.by_topic(APPROVAL_RESOLVED_TOPIC)
    assert len(resolved) == 1


async def test_deploy_gate_authorizes_but_never_deploys() -> None:
    """J4/AC#15 — deploy gate approve carries the signal; no execution here."""
    service, _, bus, _ = build_stack()
    created = await service.create(
        workspace_id=WS,
        gate_type=GateType.DEPLOY,
        subject_type="deployment",
        subject_id=uuid.uuid4(),
        requested_actor="system",
        gate_payload={"environment": "production", "restricted_environment": True},
    )
    context = await service.get_context(created.id, workspace_id=WS)
    assert any(f.category == "restricted_env" for f in context.risk_flags)

    resolution = await service.resolve(created.id, APPROVE, ADMIN, workspace_id=WS)
    assert resolution.outcome.details["signal"] == "deploy.approved"
    assert resolution.outcome.details["executed_by"] == "downstream"
    assert len(bus.by_topic(APPROVAL_RESOLVED_TOPIC)) == 1
