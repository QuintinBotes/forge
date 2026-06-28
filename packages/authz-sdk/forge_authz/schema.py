"""Re-export of the frozen ``forge_contracts.authz`` DTOs (F30).

The resolver SDK and the contract share one object set; importing from
``forge_authz.schema`` keeps call sites decoupled from the contracts package
path while remaining the exact same classes.
"""

from __future__ import annotations

from forge_contracts.authz import (
    AccessLevel,
    EffectiveAccess,
    Permission,
    PermissionResolver,
    PrincipalContext,
    PrincipalRef,
    PrincipalType,
    ProjectTeamAccess,
    ProjectVisibility,
    ResourceRef,
    Role,
    RoleGrant,
    ScopeRef,
    ScopeType,
    TeamMembership,
    TeamRole,
)

__all__ = [
    "AccessLevel",
    "EffectiveAccess",
    "Permission",
    "PermissionResolver",
    "PrincipalContext",
    "PrincipalRef",
    "PrincipalType",
    "ProjectTeamAccess",
    "ProjectVisibility",
    "ResourceRef",
    "Role",
    "RoleGrant",
    "ScopeRef",
    "ScopeType",
    "TeamMembership",
    "TeamRole",
]
