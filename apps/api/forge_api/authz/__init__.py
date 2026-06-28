"""F30 authorization wiring for the Forge API (scoped RBAC).

Exposes the FastAPI dependencies + helpers that replace the flat
``forge_api.auth.rbac`` checks with the scoped ``forge_authz`` resolver: the
per-request :class:`~forge_contracts.authz.PrincipalContext` loader,
``require_permission`` / ``require_role`` (back-compat shim), the
``visible_project_ids`` board filter, and the service-error -> HTTP mapper.
"""

from __future__ import annotations

from forge_api.authz.deps import (
    ALL,
    AllProjects,
    get_authz_service,
    get_principal_context,
    map_authz_errors,
    require_permission,
    require_role,
    visible_project_ids,
)

__all__ = [
    "ALL",
    "AllProjects",
    "get_authz_service",
    "get_principal_context",
    "map_authz_errors",
    "require_permission",
    "require_role",
    "visible_project_ids",
]
