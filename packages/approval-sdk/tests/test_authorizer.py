"""Unit tests — the single authorization policy (F36 AC#5-#8, #13, #15)."""

from __future__ import annotations

import uuid

import pytest
from conftest import AGENT_ID, MEMBER_ID, make_principal, make_request

from forge_approval.authorizer import ApprovalAuthorizer, AuthorizationError
from forge_approval.models import (
    ApprovalAction,
    ApprovalDecisionRequest,
    ApprovalRequest,
    GateType,
    Principal,
    Role,
)
from forge_contracts.dtos import DeployRules, ReviewRules

APPROVE = ApprovalDecisionRequest(decision=ApprovalAction.APPROVE)


class FakePolicyReader:
    """Scriptable review/deploy rules + deploy permission."""

    def __init__(
        self,
        review_rules: ReviewRules | None = None,
        deploy_rules: DeployRules | None = None,
        deployers: set[uuid.UUID] | None = None,
    ) -> None:
        self._review = review_rules or ReviewRules()
        self._deploy = deploy_rules or DeployRules()
        self._deployers = deployers

    def review_rules_for(self, request: ApprovalRequest) -> ReviewRules:
        return self._review

    def deploy_rules_for(self, request: ApprovalRequest) -> DeployRules:
        return self._deploy

    def can_deploy(self, actor: Principal, environment: str | None) -> bool:
        if self._deployers is None:
            return True
        return actor.id in self._deployers


@pytest.mark.parametrize("gate_type", list(GateType))
def test_agent_and_system_never_resolve(gate_type: GateType) -> None:
    """AC#5: non-user principals are refused on every gate type."""
    authorizer = ApprovalAuthorizer()
    request = make_request(gate_type)
    agent = make_principal(kind="agent", role=Role.AGENT_RUNNER, principal_id=AGENT_ID)
    system = make_principal(kind="system", role=None, principal_id=None)
    for actor in (agent, system):
        with pytest.raises(AuthorizationError):
            authorizer.check(actor, request, APPROVE)


@pytest.mark.parametrize("gate_type", list(GateType))
def test_viewer_refused_all_gates(gate_type: GateType) -> None:
    """AC#6: viewers never resolve."""
    authorizer = ApprovalAuthorizer()
    viewer = make_principal(role=Role.VIEWER)
    with pytest.raises(AuthorizationError):
        authorizer.check(viewer, make_request(gate_type), APPROVE)


def test_member_refused_policy_override_admin_allowed() -> None:
    """AC#6: policy_override is admin-only."""
    authorizer = ApprovalAuthorizer()
    request = make_request(GateType.POLICY_OVERRIDE)
    member = make_principal(role=Role.MEMBER)
    admin = make_principal(role=Role.ADMIN)
    with pytest.raises(AuthorizationError):
        authorizer.check(member, request, APPROVE)
    authorizer.check(admin, request, APPROVE)  # no raise


@pytest.mark.parametrize(
    "gate_type",
    [GateType.SPEC, GateType.PLAN, GateType.PR, GateType.DEPLOY, GateType.INCIDENT_REMEDIATION],
)
def test_member_can_resolve_non_admin_gates(gate_type: GateType) -> None:
    authorizer = ApprovalAuthorizer()
    authorizer.check(make_principal(), make_request(gate_type), APPROVE)


def test_no_self_approval_agent_identity() -> None:
    """AC#7: the producing agent's identity can never approve — always."""
    authorizer = ApprovalAuthorizer()
    request = make_request(GateType.PR, requested_actor=f"agent:{MEMBER_ID}")
    actor = make_principal()  # same id as the producing agent
    with pytest.raises(AuthorizationError):
        authorizer.check(actor, request, APPROVE)


def test_no_self_approval_author_only_when_flag() -> None:
    """AC#7: user self-approval is refused only under forbid_self_approval."""
    request = make_request(GateType.PR, requested_actor=f"user:{MEMBER_ID}")
    lenient = ApprovalAuthorizer(forbid_self_approval=False)
    strict = ApprovalAuthorizer(forbid_self_approval=True)
    lenient.check(make_principal(), request, APPROVE)  # allowed
    with pytest.raises(AuthorizationError):
        strict.check(make_principal(), request, APPROVE)


def test_pr_review_rules_enforced() -> None:
    """AC#8: a member outside required_reviewers is refused on pr gates."""
    reader = FakePolicyReader(
        review_rules=ReviewRules(required_reviewers=[str(uuid.uuid4())])
    )
    authorizer = ApprovalAuthorizer(reader)
    with pytest.raises(AuthorizationError):
        authorizer.check(make_principal(), make_request(GateType.PR), APPROVE)

    # A listed reviewer passes.
    listed = FakePolicyReader(
        review_rules=ReviewRules(required_reviewers=[str(MEMBER_ID)])
    )
    ApprovalAuthorizer(listed).check(make_principal(), make_request(GateType.PR), APPROVE)


def test_pr_review_rules_relaxed_when_merge_approval_off() -> None:
    reader = FakePolicyReader(
        review_rules=ReviewRules(
            required_reviewers=[str(uuid.uuid4())], approval_required_for_merge=False
        )
    )
    ApprovalAuthorizer(reader).check(make_principal(), make_request(GateType.PR), APPROVE)


def test_deploy_permission_enforced() -> None:
    """AC#15: deploy gates require environment deploy permission."""
    reader = FakePolicyReader(deployers=set())  # nobody may deploy
    authorizer = ApprovalAuthorizer(reader)
    request = make_request(
        GateType.DEPLOY, gate_payload={"environment": "production", "restricted_environment": True}
    )
    with pytest.raises(AuthorizationError) as err:
        authorizer.check(make_principal(), request, APPROVE)
    assert "deploy permission" in str(err.value)

    permitted = FakePolicyReader(deployers={MEMBER_ID})
    ApprovalAuthorizer(permitted).check(make_principal(), request, APPROVE)


def test_escalated_gate_requires_admin() -> None:
    """AC#13: escalation raises the resolving bar to admin."""
    authorizer = ApprovalAuthorizer()
    request = make_request(GateType.INCIDENT_REMEDIATION, escalated=True)
    with pytest.raises(AuthorizationError):
        authorizer.check(make_principal(), request, APPROVE)
    authorizer.check(make_principal(role=Role.ADMIN), request, APPROVE)


def test_authorization_error_carries_reason() -> None:
    authorizer = ApprovalAuthorizer()
    with pytest.raises(AuthorizationError) as err:
        authorizer.check(
            make_principal(kind="agent", role=Role.AGENT_RUNNER),
            make_request(GateType.PR),
            APPROVE,
        )
    assert err.value.reason
