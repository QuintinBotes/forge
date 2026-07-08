"""Per-role model+effort config resolver (Adaptive Orchestration: ao-config).

Merges the hardcoded :data:`~forge_contracts.DEFAULT_ROLE_CONFIG` with a
workspace/project override read through the storage-boundary
:class:`~forge_contracts.RoleConfigStore` Protocol (implemented by
``forge_db``). Precedence, most-specific wins: **project override > workspace
override > hardcoded default**.

This module takes no dependency on ``forge_db``: it only type-checks against
the ``forge_contracts`` Protocol, so any conforming store (a real SQL-backed
one, or a fake/in-memory double in tests) resolves the same way.
"""

from __future__ import annotations

from uuid import UUID

from forge_contracts.orchestration_config import (
    DEFAULT_ROLE_CONFIG,
    AgentRole,
    EffectiveRoleConfig,
    RoleConfigStore,
)

__all__ = ["resolve_effective_config"]


def resolve_effective_config(
    store: RoleConfigStore,
    workspace_id: UUID,
    role: AgentRole,
    *,
    project_id: UUID | None = None,
) -> EffectiveRoleConfig:
    """Resolve the effective ``{model_or_tier, effort}`` for ``role``.

    Reads at most two rows from ``store`` (project-scoped, then workspace-wide)
    and falls back to :data:`~forge_contracts.DEFAULT_ROLE_CONFIG` when neither
    exists. ``project_id=None`` skips the project-scoped lookup entirely (there
    is no project to scope to), so it resolves workspace-vs-default only.
    """
    if project_id is not None:
        project_override = store.get_override(workspace_id, role, project_id=project_id)
        if project_override is not None:
            return EffectiveRoleConfig(
                role=role,
                model_or_tier=project_override.model_or_tier,
                effort=project_override.effort,
                source="project",
            )

    workspace_override = store.get_override(workspace_id, role, project_id=None)
    if workspace_override is not None:
        return EffectiveRoleConfig(
            role=role,
            model_or_tier=workspace_override.model_or_tier,
            effort=workspace_override.effort,
            source="workspace",
        )

    default = DEFAULT_ROLE_CONFIG[role]
    return EffectiveRoleConfig(
        role=role,
        model_or_tier=default.model_or_tier,
        effort=default.effort,
        source="default",
    )
