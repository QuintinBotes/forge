"""The single server-side authorization policy for resolving approval gates.

Every surface (API, Slack, UI) resolves through :meth:`ApprovalAuthorizer.check`
so the rules cannot be bypassed by choosing a different surface:

- agents and system principals never resolve (Build-Prompt constraint #2);
- viewers never resolve;
- ``policy_override`` is admin-only; escalation raises any gate's bar to admin;
- ``pr`` honours repo ``review_rules``; ``deploy`` requires deploy permission;
- optional no-self-approval (and an agent's author can never approve its run).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from forge_approval.models import (
    ApprovalDecisionRequest,
    ApprovalRequest,
    GateType,
    Principal,
    Role,
)
from forge_contracts.dtos import DeployRules, ReviewRules


class AuthorizationError(Exception):
    """Refusal to let a principal act on a gate (maps to HTTP 403)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@runtime_checkable
class PolicyReader(Protocol):
    """Supplies repo ``review_rules`` / ``deploy_rules`` for a gate's subject."""

    def review_rules_for(self, request: ApprovalRequest) -> ReviewRules: ...

    def deploy_rules_for(self, request: ApprovalRequest) -> DeployRules: ...

    def can_deploy(self, actor: Principal, environment: str | None) -> bool: ...


class DefaultPolicyReader:
    """Spec defaults: default rules; workspace members hold deploy permission."""

    def review_rules_for(self, request: ApprovalRequest) -> ReviewRules:
        return ReviewRules()

    def deploy_rules_for(self, request: ApprovalRequest) -> DeployRules:
        return DeployRules()

    def can_deploy(self, actor: Principal, environment: str | None) -> bool:
        return actor.role in (Role.ADMIN, Role.MEMBER)


#: Minimum role required to RESOLVE each gate (escalation raises to ADMIN).
GATE_MIN_ROLE: dict[GateType, Role] = {
    GateType.SPEC: Role.MEMBER,
    GateType.PLAN: Role.MEMBER,
    GateType.PR: Role.MEMBER,
    GateType.DEPLOY: Role.MEMBER,  # + deploy-permission check
    GateType.INCIDENT_REMEDIATION: Role.MEMBER,  # admin once escalated
    GateType.POLICY_OVERRIDE: Role.ADMIN,  # always admin
}

#: Role ranks for the minimum-role comparison. ``agent-runner`` and ``viewer``
#: deliberately rank below every gate's minimum: they can never resolve.
_ROLE_RANK: dict[Role, int] = {
    Role.VIEWER: 0,
    Role.AGENT_RUNNER: 0,
    Role.MEMBER: 1,
    Role.ADMIN: 2,
}


class ApprovalAuthorizer:
    """The one authorization policy (AC#5-#8, #13, #15)."""

    def __init__(
        self,
        policy_reader: PolicyReader | None = None,
        *,
        forbid_self_approval: bool = False,
    ) -> None:
        self._policy = policy_reader or DefaultPolicyReader()
        self._forbid_self_approval = forbid_self_approval

    def check(
        self,
        actor: Principal,
        request: ApprovalRequest,
        decision: ApprovalDecisionRequest,
    ) -> None:
        """Raise :class:`AuthorizationError` unless every rule holds."""
        if actor.kind != "user":
            raise AuthorizationError(
                f"{actor.kind} principals can never resolve approval gates"
            )
        if actor.role is None or _ROLE_RANK.get(actor.role, 0) < 1:
            raise AuthorizationError(
                f"role '{actor.role.value if actor.role else 'none'}' cannot resolve"
                " approval gates"
            )

        required = GATE_MIN_ROLE[request.gate_type]
        if request.escalated:
            required = Role.ADMIN
        if _ROLE_RANK[actor.role] < _ROLE_RANK[required]:
            raise AuthorizationError(
                f"gate '{request.gate_type.value}' requires role"
                f" '{required.value}' to resolve (actor is '{actor.role.value}')"
            )

        if request.gate_type is GateType.PR:
            self._check_review_rules(actor, request)
        if request.gate_type is GateType.DEPLOY:
            environment = request.gate_payload.get("environment")
            env = environment if isinstance(environment, str) else None
            if not self._policy.can_deploy(actor, env):
                raise AuthorizationError(
                    f"actor lacks deploy permission for environment '{env or 'unknown'}'"
                )

        self._check_self_approval(actor, request)

    # ------------------------------------------------------------------ #

    def _check_review_rules(self, actor: Principal, request: ApprovalRequest) -> None:
        rules = self._policy.review_rules_for(request)
        if not rules.approval_required_for_merge:
            return
        reviewers = rules.required_reviewers
        if reviewers and str(actor.id) not in reviewers:
            raise AuthorizationError(
                "repo review_rules restrict this pr gate to its required reviewers"
            )

    def _check_self_approval(self, actor: Principal, request: ApprovalRequest) -> None:
        # The producing agent's identity can never approve its own run's gate,
        # regardless of configuration.
        if request.requested_actor == f"agent:{actor.id}":
            raise AuthorizationError(
                "the agent identity that produced this run cannot approve its gate"
            )
        if self._forbid_self_approval and request.requested_actor == f"user:{actor.id}":
            raise AuthorizationError("self-approval of one's own request is forbidden")


__all__ = [
    "GATE_MIN_ROLE",
    "ApprovalAuthorizer",
    "AuthorizationError",
    "DefaultPolicyReader",
    "PolicyReader",
]
