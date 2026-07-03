"""Unit tests — the F36-owned deploy + policy_override gate primitives (J4/J5)."""

from __future__ import annotations

import uuid
from datetime import timedelta

from conftest import ADMIN_ID, make_principal, make_request

from forge_approval.models import (
    ApprovalAction,
    ApprovalDecisionRequest,
    GateType,
    Role,
)
from forge_approval.providers.deploy import (
    DEPLOY_APPROVED_SIGNAL,
    DeployGateProvider,
    DeployResolutionHook,
)
from forge_approval.providers.policy_override import (
    POLICY_OVERRIDE_GRANTED_SIGNAL,
    InMemoryGrantStore,
    PolicyOverrideGateProvider,
    PolicyOverrideResolutionHook,
    action_fingerprint,
)

APPROVE = ApprovalDecisionRequest(decision=ApprovalAction.APPROVE)
REJECT = ApprovalDecisionRequest(decision=ApprovalAction.REJECT)
ADMIN = make_principal(role=Role.ADMIN, principal_id=ADMIN_ID)


# --------------------------------------------------------------------------- #
# deploy                                                                       #
# --------------------------------------------------------------------------- #


async def test_deploy_context_flags_restricted_env() -> None:
    """AC#15: a restricted-env deploy gate carries a restricted_env risk flag."""
    request = make_request(
        GateType.DEPLOY,
        gate_payload={
            "environment": "production",
            "restricted_environment": True,
            "source_commit": "abc1234",
            "diff": {"files_changed": 3},
        },
    )
    context = await DeployGateProvider().build_context(request)
    categories = {flag.category for flag in context.risk_flags}
    assert "restricted_env" in categories
    assert any(f.severity == "critical" for f in context.risk_flags)
    assert context.diff == {"files_changed": 3}
    assert ApprovalAction.ESCALATE not in context.available_actions


async def test_deploy_approve_signals_but_never_executes() -> None:
    """AC#15: approve emits the deploy.approved signal; execution is downstream."""
    request = make_request(GateType.DEPLOY, gate_payload={"environment": "production"})
    outcome = await DeployResolutionHook().on_resolved(request, APPROVE, ADMIN)
    assert outcome.completed is True
    assert outcome.follow_up_state == "deploy_approved"
    assert outcome.details["signal"] == DEPLOY_APPROVED_SIGNAL
    assert outcome.details["executed_by"] == "downstream"


async def test_deploy_reject_denies() -> None:
    request = make_request(GateType.DEPLOY, gate_payload={"environment": "production"})
    outcome = await DeployResolutionHook().on_resolved(request, REJECT, ADMIN)
    assert outcome.follow_up_state == "deploy_denied"


# --------------------------------------------------------------------------- #
# policy_override                                                              #
# --------------------------------------------------------------------------- #


def _override_request(**over):
    fingerprint = action_fingerprint({"tool": "shell", "action": "deploy_prod"})
    defaults = {
        "agent_run_id": uuid.uuid4(),
        "gate_payload": {
            "action": {"tool": "shell", "action": "deploy_prod"},
            "blocked_by": ["deny_prod_deploys"],
            "severity": "critical",
            "rationale": "the task requires a prod migration",
            "action_fingerprint": fingerprint,
        },
    }
    defaults.update(over)
    return make_request(GateType.POLICY_OVERRIDE, **defaults), fingerprint


async def test_override_context_centers_action_and_rules() -> None:
    """AC#3: override context shows the attempted action + blocking rules."""
    request, fingerprint = _override_request()
    context = await PolicyOverrideGateProvider().build_context(request)
    assert context.gate_payload["action"] == {"tool": "shell", "action": "deploy_prod"}
    assert context.gate_payload["blocked_by"] == ["deny_prod_deploys"]
    assert context.gate_payload["action_fingerprint"] == fingerprint
    assert any("deny_prod_deploys" in flag.message for flag in context.risk_flags)
    assert ApprovalAction.ESCALATE in context.available_actions
    # Sections that don't apply to an override gate stay hidden.
    assert context.diff is None
    assert context.verification is None
    assert context.traceability is None


async def test_override_approve_mints_single_use_grant() -> None:
    """AC#14: approve mints one grant; consume True once, then False."""
    grants = InMemoryGrantStore()
    hook = PolicyOverrideResolutionHook(grants)
    request, fingerprint = _override_request()

    outcome = await hook.on_resolved(request, APPROVE, ADMIN)
    assert outcome.completed is True
    assert outcome.details["signal"] == POLICY_OVERRIDE_GRANTED_SIGNAL
    assert outcome.details["single_use"] is True

    assert request.agent_run_id is not None
    assert (
        await grants.consume(
            agent_run_id=request.agent_run_id, action_fingerprint=fingerprint
        )
        is True
    )
    assert (
        await grants.consume(
            agent_run_id=request.agent_run_id, action_fingerprint=fingerprint
        )
        is False
    )


async def test_override_grant_ttl_is_short() -> None:
    grants = InMemoryGrantStore()
    hook = PolicyOverrideResolutionHook(grants, ttl=timedelta(minutes=1))
    request, _ = _override_request()
    await hook.on_resolved(request, APPROVE, ADMIN)
    grant = grants.all()[0]
    assert grant.granted_by == ADMIN_ID
    assert grant.consumed is False


async def test_override_reject_routes_to_human() -> None:
    """J5: reject denies the call and routes to needs_human_input."""
    grants = InMemoryGrantStore()
    hook = PolicyOverrideResolutionHook(grants)
    request, fingerprint = _override_request()
    outcome = await hook.on_resolved(request, REJECT, ADMIN)
    assert outcome.follow_up_state == "needs_human_input"
    assert grants.all() == []  # no grant minted on reject
    assert request.agent_run_id is not None
    assert (
        await grants.consume(
            agent_run_id=request.agent_run_id, action_fingerprint=fingerprint
        )
        is False
    )


async def test_override_approve_without_fingerprint_blocks() -> None:
    grants = InMemoryGrantStore()
    hook = PolicyOverrideResolutionHook(grants)
    request, _ = _override_request(gate_payload={"action": {}})
    outcome = await hook.on_resolved(request, APPROVE, ADMIN)
    assert outcome.completed is False
    assert outcome.blocking_reasons
    assert grants.all() == []
