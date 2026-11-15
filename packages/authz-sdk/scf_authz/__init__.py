"""scf-authz — pure RBAC permission resolver for SCF (F30).

No FastAPI / SQLAlchemy imports (mirrors ``scf_policy``). Consumes the frozen
``scf_contracts.authz`` DTOs and computes effective access under a documented,
deterministic precedence rule.
"""

from __future__ import annotations

from scf_authz.errors import (
    AccessDenied,
    AuthzError,
    EscalationError,
    LastAdminError,
    TeamCycleError,
    TeamDepthError,
)
from scf_authz.permissions import (
    ACCESS_LEVEL_ROLE,
    ROLE_PERMISSIONS,
    ROLE_RANK,
    WORKSPACE_ONLY_PERMISSIONS,
    scope_narrow,
)
from scf_authz.resolver import (
    MAX_TEAM_DEPTH,
    DefaultPermissionResolver,
    check_grant_allowed,
    ensure_not_last_admin,
    max_role_at_scope,
    team_closure,
    validate_team_parent,
)

__version__ = "0.1.0"

__all__ = [
    "ACCESS_LEVEL_ROLE",
    "MAX_TEAM_DEPTH",
    "ROLE_PERMISSIONS",
    "ROLE_RANK",
    "WORKSPACE_ONLY_PERMISSIONS",
    "AccessDenied",
    "AuthzError",
    "DefaultPermissionResolver",
    "EscalationError",
    "LastAdminError",
    "TeamCycleError",
    "TeamDepthError",
    "__version__",
    "check_grant_allowed",
    "ensure_not_last_admin",
    "max_role_at_scope",
    "scope_narrow",
    "team_closure",
    "validate_team_parent",
]
