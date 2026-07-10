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

from collections.abc import Iterable
from typing import Protocol, runtime_checkable
from uuid import UUID

from forge_approval.codeowners import parse_codeowners, required_owners_for_paths
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
    """Spec defaults, with the gate's own ``review_rules`` honoured when present.

    A PR-gate producer may stamp the repo's resolved ``review_rules`` into the
    gate payload (``gate_payload['review_rules']``); this reader surfaces them so
    the single authorizer applies the repo's ``min_approvals`` quorum and
    ``require_code_owners`` rule without a separate policy round-trip. Absent that,
    the spec defaults apply. Workspace members hold deploy permission.
    """

    def review_rules_for(self, request: ApprovalRequest) -> ReviewRules:
        raw = request.gate_payload.get("review_rules")
        if isinstance(raw, dict):
            try:
                return ReviewRules.model_validate(raw)
            except ValueError:
                return ReviewRules()
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
            raise AuthorizationError(f"{actor.kind} principals can never resolve approval gates")
        if actor.role is None or _ROLE_RANK.get(actor.role, 0) < 1:
            raise AuthorizationError(
                f"role '{actor.role.value if actor.role else 'none'}' cannot resolve approval gates"
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
            self._check_code_owners(actor, request)
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

    def min_approvals_for(self, request: ApprovalRequest) -> int:
        """The distinct-approver quorum this gate needs, from repo ``review_rules``.

        Maps ``review_rules.min_approvals`` onto the gate so a policy raising the
        bar above 1 turns the PR gate into a quorum (via :func:`required_approvals`).
        Non-PR gates never carry a policy quorum (``1``).
        """
        if request.gate_type is not GateType.PR:
            return 1
        return required_approvals(self._policy.review_rules_for(request))

    def _check_code_owners(self, actor: Principal, request: ApprovalRequest) -> None:
        """Enforce path-scoped ``CODEOWNERS`` approval for a PR gate.

        Active only when the repo ``review_rules.require_code_owners`` is set and
        the gate payload carries the repo's ``codeowners`` text plus the change's
        ``changed_paths``. The resolving actor must be an owner of the changed
        paths; owners are matched against the actor's id (the same convention as
        ``required_reviewers``), optionally via an ``owner_identities`` map that
        resolves a CODEOWNERS handle to a Forge user id.
        """
        rules = self._policy.review_rules_for(request)
        if not getattr(rules, "require_code_owners", False):
            return
        payload = request.gate_payload
        text = payload.get("codeowners")
        if not isinstance(text, str) or not text.strip():
            return
        paths = [p for p in payload.get("changed_paths", []) if isinstance(p, str)]
        required = required_owners_for_paths(parse_codeowners(text), paths)
        if not required:
            return
        identities = payload.get("owner_identities")
        identities = identities if isinstance(identities, dict) else {}
        if not self._actor_is_owner(actor, required, identities):
            raise AuthorizationError(
                "repo CODEOWNERS require an owner of the changed paths to approve this pr gate"
            )

    @staticmethod
    def _actor_is_owner(
        actor: Principal, required_owners: list[str], identities: dict[str, str]
    ) -> bool:
        actor_id = str(actor.id)
        for owner in required_owners:
            if owner == actor_id or owner.lstrip("@") == actor_id:
                return True
            if identities.get(owner) == actor_id:
                return True
        return False

    def _check_self_approval(self, actor: Principal, request: ApprovalRequest) -> None:
        # The producing agent's identity can never approve its own run's gate,
        # regardless of configuration.
        if request.requested_actor == f"agent:{actor.id}":
            raise AuthorizationError(
                "the agent identity that produced this run cannot approve its gate"
            )
        if self._forbid_self_approval and request.requested_actor == f"user:{actor.id}":
            raise AuthorizationError("self-approval of one's own request is forbidden")


def required_approvals(review_rules: ReviewRules | None) -> int:
    """The number of distinct approvals a PR gate needs (``min_approvals`` >= 1).

    A repo raising ``review_rules.min_approvals`` above 1 turns the PR gate into a
    multi-approver quorum: a single approve no longer resolves it.
    """
    if review_rules is None:
        return 1
    return max(1, int(getattr(review_rules, "min_approvals", 1) or 1))


def quorum_met(approver_ids: Iterable[UUID], required: int) -> bool:
    """True once ``required`` *distinct* approvers have approved (min 1)."""
    return len(set(approver_ids)) >= max(1, required)


__all__ = [
    "GATE_MIN_ROLE",
    "ApprovalAuthorizer",
    "AuthorizationError",
    "DefaultPolicyReader",
    "PolicyReader",
    "quorum_met",
    "required_approvals",
]
