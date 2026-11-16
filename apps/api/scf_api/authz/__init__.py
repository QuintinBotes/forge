"""F30 authorization wiring for the SCF API (scoped RBAC).

Exposes the FastAPI dependencies + helpers that replace the flat
``scf_api.auth.rbac`` checks with the scoped ``scf_authz`` resolver: the
per-request :class:`~scf_contracts.authz.PrincipalContext` loader,
``require_permission`` / ``require_role`` (back-compat shim), the
``visible_project_ids`` board filter, and the service-error -> HTTP mapper.
"""

from __future__ import annotations

from scf_api.authz.deps import (
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
