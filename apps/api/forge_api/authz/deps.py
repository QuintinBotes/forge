"""FastAPI dependencies + helpers for F30 scoped RBAC.

``require_permission(permission)`` is the workspace-scoped gate (most authz
routes resolve workspace-level permissions; project/team-scoped checks happen in
the service against the loaded resource). ``require_role(min_role)`` is the
back-compat shim mapping the v1 flat :class:`UserRole` to its workspace-scope
permission. ``visible_project_ids`` is the single sanctioned board filter.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import Depends, HTTPException, status
from forge_authz.errors import (
    AccessDenied,
    EscalationError,
    LastAdminError,
    TeamCycleError,
    TeamDepthError,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from forge_api.deps import DbSession, Principal, get_current_principal
from forge_api.services.authz_service import AuthzService, ResourceNotFound
from forge_contracts.authz import Permission, PrincipalContext, ResourceRef
from forge_contracts.enums import UserRole
from forge_db.models import Project


class _AllProjects:
    """Sentinel meaning "every project is visible" (workspace admin)."""

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "ALL"


AllProjects = _AllProjects
ALL = _AllProjects()


def get_authz_service(session: DbSession) -> AuthzService:
    """Build the request-scoped authz service (DB + audit on the same session)."""
    return AuthzService(session)


AuthzServiceDep = Annotated[AuthzService, Depends(get_authz_service)]
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


def get_principal_context(
    principal: CurrentPrincipal, service: AuthzServiceDep
) -> PrincipalContext:
    """Load the caller's :class:`PrincipalContext` (grants + memberships) once."""
    return service.load_principal_context(principal.workspace_id, principal.user_id)


PrincipalContextDep = Annotated[PrincipalContext, Depends(get_principal_context)]


def _forbidden(permission: Permission, scope_type: str, scope_id: uuid.UUID) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "error": "forbidden",
            "missing_permission": permission.value,
            "scope": {"type": scope_type, "id": str(scope_id)},
        },
    )


def require_permission(permission: Permission) -> Callable[..., PrincipalContext]:
    """Workspace-scoped authorization dependency.

    Resolves the caller's effective access at the workspace resource and raises
    403 (with the missing permission + scope) when ``permission`` is absent.
    Returns the loaded :class:`PrincipalContext` so handlers reuse it.
    """

    def _dependency(ctx: PrincipalContextDep, service: AuthzServiceDep) -> PrincipalContext:
        resource = ResourceRef(workspace_id=ctx.workspace_id)
        eff = service.resolve(ctx, resource)
        if permission not in eff.permissions:
            raise _forbidden(permission, "workspace", ctx.workspace_id)
        return ctx

    return _dependency


#: The v1 flat role -> its representative workspace-scope permission (AC21 shim).
_ROLE_PERMISSION: dict[UserRole, Permission] = {
    UserRole.ADMIN: Permission.WORKSPACE_ADMIN,
    UserRole.MEMBER: Permission.PROJECT_WRITE,
    UserRole.VIEWER: Permission.PROJECT_READ,
    UserRole.AGENT_RUNNER: Permission.AGENT_RUN,
}


def require_role(min_role: UserRole) -> Callable[..., PrincipalContext]:
    """Back-compat shim: ``require_role(UserRole.X)`` -> ``require_permission`` of
    the equivalent workspace-scope permission (F30 AC21)."""
    return require_permission(_ROLE_PERMISSION[min_role])


def visible_project_ids(
    ctx: PrincipalContext, workspace_id: uuid.UUID, service: AuthzService
) -> set[uuid.UUID] | _AllProjects:
    """Project ids the principal can ``project.read``; :data:`ALL` for ws admin.

    Board list/query endpoints AND this into their ``workspace_id`` filter.
    """
    ws_eff = service.resolve(ctx, ResourceRef(workspace_id=workspace_id))
    if Permission.WORKSPACE_ADMIN in ws_eff.permissions:
        return ALL
    session: Session = service.session
    visible: set[uuid.UUID] = set()
    for project in session.scalars(
        select(Project).where(Project.workspace_id == workspace_id)
    ).all():
        resource = service.project_resource(workspace_id, project.id)
        if Permission.PROJECT_READ in service.resolve(ctx, resource).permissions:
            visible.add(project.id)
    return visible


@contextmanager
def map_authz_errors() -> Iterator[None]:
    """Translate authz domain errors into the F30 HTTP error contract."""
    try:
        yield
    except ResourceNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found") from exc
    except AccessDenied as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "forbidden",
                "missing_permission": exc.permission.value,
                "scope": {
                    "type": exc.scope_type.value,
                    "id": str(exc.scope_id) if exc.scope_id else None,
                },
            },
        ) from exc
    except EscalationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "escalation",
                "granted_role": exc.granted_role.value,
                "actor_max_role": exc.actor_max_role.value if exc.actor_max_role else None,
            },
        ) from exc
    except LastAdminError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail={"error": "last_admin"}
        ) from exc
    except TeamCycleError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "team_cycle", "path": [str(p) for p in exc.path]},
        ) from exc
    except TeamDepthError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "team_depth_exceeded"},
        ) from exc


__all__ = [
    "ALL",
    "AllProjects",
    "AuthzServiceDep",
    "PrincipalContextDep",
    "get_authz_service",
    "get_principal_context",
    "map_authz_errors",
    "require_permission",
    "require_role",
    "visible_project_ids",
]
