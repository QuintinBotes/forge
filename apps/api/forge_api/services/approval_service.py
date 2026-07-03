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

from forge_api.deps import Principal
from forge_api.observability.redaction import redact_mapping
from forge_api.observability.service import get_observability_service
from forge_approval import (
    ApprovalAuthorizer,
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
    InMemoryGrantStore,
    PolicyOverrideGateProvider,
    PolicyOverrideResolutionHook,
)
from forge_contracts import UserRole


def build_gate_registry(grants: InMemoryGrantStore) -> GateRegistry:
    """Register every available provider/hook (the F36 composition root)."""
    registry = GateRegistry()
    registry.register_provider(DeployGateProvider())
    registry.register_hook(DeployResolutionHook())
    registry.register_provider(PolicyOverrideGateProvider())
    registry.register_hook(PolicyOverrideResolutionHook(grants))
    return registry


@lru_cache(maxsize=1)
def get_override_grant_store() -> InMemoryGrantStore:
    """Process-wide single-use override-grant store (J5 consume contract)."""
    return InMemoryGrantStore()


@lru_cache(maxsize=1)
def get_gate_registry() -> GateRegistry:
    """Process-wide gate registry (override in tests via DI)."""
    return build_gate_registry(get_override_grant_store())


@lru_cache(maxsize=1)
def get_approval_service() -> ApprovalService:
    """Process-wide unified approval service (override in tests via DI)."""
    return ApprovalService(
        InMemoryApprovalRepository(),
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
    kind = "agent" if principal.role is UserRole.AGENT_RUNNER else "user"
    return ApprovalPrincipal(
        kind=kind,
        id=principal.user_id,
        role=ApprovalRole(principal.role.value),
        workspace_id=principal.workspace_id,
    )


__all__ = [
    "build_gate_registry",
    "get_approval_service",
    "get_gate_registry",
    "get_override_grant_store",
    "to_approval_principal",
]
