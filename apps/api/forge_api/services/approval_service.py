"""F36 composition root: the unified Human Approval System service.

Builds the process-wide ``forge_approval.ApprovalService`` with:

* the in-memory :class:`~forge_approval.repository.InMemoryApprovalRepository`
  (foundation precedent — the DB-backed repository swaps in behind the same
  Protocol; the canonical tables ship in ``forge_db.models.approval``);
* the process :class:`GateRegistry` with the F36-owned ``deploy`` and
  ``policy_override`` providers/hooks registered at startup (pr/spec/plan/
  incident providers are registered here by their owning slices when they
  land; missing providers degrade to a read-only context, and resolving a
  hook-less gate returns a ``not_implemented`` outcome — never a crash);
* the single :class:`ApprovalAuthorizer` (agents/viewers never resolve,
  ``policy_override`` is admin-only, review/deploy rules apply);
* the foundation redactor (``forge_api.observability.redaction``) applied to
  every ``gate_payload`` before persist/audit/emit;
* the process-wide observability :class:`AuditLog` (immutable, hash-chained)
  as the audit sink, so approval writes appear in ``/observability`` queries.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from forge_api.deps import Principal
from forge_api.observability.redaction import redact_mapping
from forge_api.observability.service import get_observability_service
from forge_api.services.authz_service import AuthzService
from forge_api.services.workflow_gate_hook import EngineFactory
from forge_approval import (
    ApprovalAuthorizer,
    ApprovalRepository,
    ApprovalService,
    GateRegistry,
    InMemoryActivityBus,
    InMemoryApprovalRepository,
)
from forge_approval.models import Principal as ApprovalPrincipal
from forge_approval.models import Role as ApprovalRole
from forge_approval.providers import (
    DeployGateProvider,
    DeployResolutionHook,
    GrantStore,
    InMemoryGrantStore,
    PolicyOverrideGateProvider,
    PolicyOverrideResolutionHook,
)
from forge_authz import ROLE_RANK, max_role_at_scope
from forge_contracts import UserRole
from forge_contracts.authz import ScopeType
from forge_db.models import User
from forge_db.models.enums import PrincipalType as DbPrincipalType
from forge_db.models.enums import ScopeType as DbScopeType
from forge_db.models.role_grant import RoleGrant


def build_gate_registry(
    grants: GrantStore, *, engine_factory: EngineFactory | None = None
) -> GateRegistry:
    """Register every available provider/hook (the F36 composition root)."""
    from forge_api.services.attestation_service import PrAttestationResolutionHook
    from forge_api.services.workflow_gate_hook import (
        PlanWorkflowGateHook,
        PrWorkflowGateHook,
        SpecWorkflowGateHook,
    )

    registry = GateRegistry()
    registry.register_provider(DeployGateProvider())
    registry.register_hook(DeployResolutionHook())
    registry.register_provider(PolicyOverrideGateProvider())
    registry.register_hook(PolicyOverrideResolutionHook(grants))
    # Spec K P0 (A3): feature-run gates (spec/plan/pr) advance their run when
    # the approval resolves. Each workflow gate hook wraps the gate's existing
    # hook — for ``pr`` the Attested Changesets hook still runs first,
    # unchanged (approving a ``pr`` gate that carries a workflow_run_id signs +
    # records a provenance attestation; it opens its own DB session via the
    # shared factory). ``engine_factory`` defaults to the process-wide engine;
    # tests inject the instance their runs live in.
    registry.register_hook(
        PrWorkflowGateHook(
            wrapped=PrAttestationResolutionHook(_attestation_session),
            engine_factory=engine_factory,
        )
    )
    registry.register_hook(SpecWorkflowGateHook(engine_factory=engine_factory))
    registry.register_hook(PlanWorkflowGateHook(engine_factory=engine_factory))
    return registry


def _attestation_session() -> Session:
    """Open a DB session for the PR attestation hook (lazy — no DB at import)."""
    from forge_api.db import get_session_factory

    return get_session_factory()()


@lru_cache(maxsize=1)
def get_override_grant_store() -> GrantStore:
    """Return the override-grant store selected by ``FORGE_OVERRIDE_GRANT_BACKEND``.

    ``memory`` (default) -> the hermetic :class:`InMemoryGrantStore` (unit-test
    default, no Postgres); ``db`` -> the durable
    :class:`~forge_api.services.policy_override_grant_store_db.DbGrantStore` bound
    to the shared session factory. Both satisfy the same ``mint`` / ``consume`` /
    ``all`` grant-store seam, so the swap is behaviour-preserving (single-active,
    single-use, TTL-expiry).
    """
    from forge_api.settings import get_settings

    if get_settings().override_grant_backend == "db":
        from forge_api.db import get_session_factory
        from forge_api.services.policy_override_grant_store_db import DbGrantStore

        return DbGrantStore(get_session_factory())
    return InMemoryGrantStore()


@lru_cache(maxsize=1)
def get_gate_registry() -> GateRegistry:
    """Process-wide gate registry (override in tests via DI)."""
    return build_gate_registry(get_override_grant_store())


def build_approval_repository() -> ApprovalRepository:
    """Return the approval repository selected by ``FORGE_APPROVAL_BACKEND``.

    ``memory`` (default) -> the hermetic :class:`InMemoryApprovalRepository`
    (unit-test default, no Postgres); ``db`` -> the durable
    :class:`~forge_api.services.approval_repository_db.SqlAlchemyApprovalRepository`
    bound to the shared session factory. Both satisfy the same async
    ``ApprovalRepository`` protocol, so the swap is behaviour-preserving.
    """
    from forge_api.settings import get_settings

    if get_settings().approval_backend == "db":
        from forge_api.db import get_session_factory
        from forge_api.services.approval_repository_db import (
            SqlAlchemyApprovalRepository,
        )

        return SqlAlchemyApprovalRepository(get_session_factory())
    return InMemoryApprovalRepository()


@lru_cache(maxsize=1)
def get_approval_service() -> ApprovalService:
    """Process-wide unified approval service (override in tests via DI)."""
    return ApprovalService(
        build_approval_repository(),
        get_gate_registry(),
        ApprovalAuthorizer(),
        events=InMemoryActivityBus(),
        audit=get_observability_service().audit,
        redactor=redact_mapping,
    )


def to_approval_principal(principal: Principal) -> ApprovalPrincipal:
    """Map the authenticated API principal onto the domain ``Principal``.

    The ``agent-runner`` role IS the agent identity at this boundary — it maps
    to ``kind="agent"`` so the single authorizer structurally refuses it on
    every gate (Build-Prompt constraint #2), regardless of any router-level
    permission checks.

    Only a person decides (ADR-0005, spec L §6a): an API key that is not one
    (``decides`` unset: unbound, or narrowed without ``approvals:decide``) maps
    to ``kind="system"``, which the authorizer refuses. :func:`to_decider` adds
    the owner's live role. Session principals are unchanged.
    """
    kind: Literal["user", "agent", "system"] = "user"
    if principal.role is UserRole.AGENT_RUNNER:
        kind = "agent"
    elif principal.auth_method == "api_key" and not principal.decides:
        kind = "system"
    return ApprovalPrincipal(
        kind=kind,
        id=principal.user_id,
        role=ApprovalRole(principal.role.value),
        workspace_id=principal.workspace_id,
        surface=principal.surface,
        credential_id=principal.credential_id,
    )


def _lower(a: UserRole, b: UserRole) -> UserRole:
    return min(a, b, key=ROLE_RANK.__getitem__)


def owner_role(session: Session, principal: Principal) -> UserRole | None:
    """The current role of the person an API key acts for; ``None`` if they have none.

    ``User.role``, lowered by the owner's live workspace grants (read as the F30
    authorizer reads them). An owner whose workspace grants have all expired has
    lost access (``None``). An owner never granted one (seed, SSO and SCIM
    provisioning write none) is governed by ``User.role``; removing a person is
    deactivation, which also revokes their keys.
    """
    user = session.get(User, principal.user_id)
    if user is None or not user.is_active or user.workspace_id != principal.workspace_id:
        return None
    role = UserRole(user.role.value)
    ctx = AuthzService(session).load_principal_context(principal.workspace_id, principal.user_id)
    granted = max_role_at_scope(ctx.grants, ScopeType.WORKSPACE, principal.workspace_id)
    if granted is not None:
        return _lower(role, granted)
    ever_granted = session.scalar(
        select(RoleGrant.id)
        .where(
            RoleGrant.workspace_id == principal.workspace_id,
            RoleGrant.principal_type == DbPrincipalType.USER,
            RoleGrant.principal_id == principal.user_id,
            RoleGrant.scope_type == DbScopeType.WORKSPACE,
        )
        .limit(1)
    )
    return None if ever_granted is not None else role


def to_decider(principal: Principal, session: Session) -> ApprovalPrincipal:
    """:func:`to_approval_principal` plus an API key's live role check (spec L §6a).

    A key decides with the lower of its own role and its owner's current role
    (a narrowed key carries none, so its owner's governs): demoting or
    deactivating the owner takes effect on the key's next decision.
    """
    actor = to_approval_principal(principal)
    if principal.auth_method != "api_key" or actor.kind != "user":
        return actor
    current = owner_role(session, principal)
    if current is None:
        return actor.model_copy(update={"kind": "system"})
    role = current if principal.narrowed else _lower(principal.role, current)
    return actor.model_copy(update={"role": ApprovalRole(role.value)})


__all__ = [
    "build_approval_repository",
    "build_gate_registry",
    "get_approval_service",
    "get_gate_registry",
    "get_override_grant_store",
    "owner_role",
    "to_approval_principal",
    "to_decider",
]
