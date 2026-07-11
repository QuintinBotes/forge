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

from sqlalchemy.orm import Session

from forge_api.deps import Principal
from forge_api.observability.redaction import redact_mapping
from forge_api.observability.service import get_observability_service
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
from forge_contracts import UserRole


def build_gate_registry(grants: GrantStore) -> GateRegistry:
    """Register every available provider/hook (the F36 composition root)."""
    from forge_api.services.attestation_service import PrAttestationResolutionHook

    registry = GateRegistry()
    registry.register_provider(DeployGateProvider())
    registry.register_hook(DeployResolutionHook())
    registry.register_provider(PolicyOverrideGateProvider())
    registry.register_hook(PolicyOverrideResolutionHook(grants))
    # Attested Changesets: approving a ``pr`` gate that carries a workflow_run_id
    # signs + records a provenance attestation. A gate without one (the unit
    # approval tests' pr gates) resolves untouched. The hook opens its own DB
    # session (the HTTP approval path threads none) via the shared factory.
    registry.register_hook(PrAttestationResolutionHook(_attestation_session))
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
    """
    kind: Literal["user", "agent", "system"] = (
        "agent" if principal.role is UserRole.AGENT_RUNNER else "user"
    )
    return ApprovalPrincipal(
        kind=kind,
        id=principal.user_id,
        role=ApprovalRole(principal.role.value),
        workspace_id=principal.workspace_id,
    )


__all__ = [
    "build_approval_repository",
    "build_gate_registry",
    "get_approval_service",
    "get_gate_registry",
    "get_override_grant_store",
    "to_approval_principal",
]
