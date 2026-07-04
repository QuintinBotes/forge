"""Access router (F30) — role grants + effective-access introspection.

``POST/DELETE /access/grants`` enforce the escalation + lockout invariants in the
service (an agent_runner / member can never grant; the last workspace admin can
never be removed). ``GET`` routes require ``role.grant`` at the scope or self.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status

from forge_api.authz import map_authz_errors
from forge_api.authz.deps import AuthzServiceDep, PrincipalContextDep
from forge_api.deps import get_current_principal
from forge_api.schemas.authz import EffectiveAccessOut, RoleGrantIn, RoleGrantOut
from forge_api.services.authz_service import AuthzService
from forge_contracts.authz import (
    Permission,
    PrincipalContext,
    PrincipalRef,
    PrincipalType,
    ResourceRef,
    RoleGrant,
    ScopeType,
)

router = APIRouter(
    prefix="/access",
    tags=["access"],
    dependencies=[Depends(get_current_principal)],
)


def _grant_out(g: RoleGrant) -> RoleGrantOut:
    return RoleGrantOut(
        id=g.id,
        workspace_id=g.workspace_id,
        principal=g.principal,
        scope=g.scope,
        role=g.role,
        expires_at=g.expires_at,
        granted_by=None,
    )


def _can_administer_grants(ctx: PrincipalContext, service: AuthzService) -> bool:
    eff = service.resolve(ctx, ResourceRef(workspace_id=ctx.workspace_id))
    return Permission.ROLE_GRANT in eff.permissions


@router.get("/grants", response_model=list[RoleGrantOut])
def list_grants(
    ctx: PrincipalContextDep,
    service: AuthzServiceDep,
    principal_id: uuid.UUID | None = None,
    scope_type: ScopeType | None = None,
    scope_id: uuid.UUID | None = None,
) -> list[RoleGrantOut]:
    is_self = principal_id is not None and principal_id == ctx.principal.id
    if not is_self and not _can_administer_grants(ctx, service):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "forbidden", "missing_permission": Permission.ROLE_GRANT.value},
        )
    grants = service.list_grants(
        ctx.workspace_id,
        principal_id=principal_id,
        scope_type=scope_type,
        scope_id=scope_id,
    )
    return [_grant_out(g) for g in grants]


@router.post("/grants", response_model=RoleGrantOut, status_code=status.HTTP_201_CREATED)
def create_grant(
    body: RoleGrantIn, ctx: PrincipalContextDep, service: AuthzServiceDep
) -> RoleGrantOut:
    with map_authz_errors():
        grant = service.grant_role(
            ctx, body.principal, body.scope, body.role, expires_at=body.expires_at
        )
        service.session.commit()
        return _grant_out(grant)


@router.delete("/grants/{grant_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_grant(grant_id: uuid.UUID, ctx: PrincipalContextDep, service: AuthzServiceDep) -> None:
    with map_authz_errors():
        service.revoke_role(ctx, grant_id)
        service.session.commit()


@router.get("/effective", response_model=EffectiveAccessOut)
def effective_access(
    ctx: PrincipalContextDep,
    service: AuthzServiceDep,
    principal_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
) -> EffectiveAccessOut:
    with map_authz_errors():
        target_id = principal_id or ctx.principal.id
        is_self = target_id == ctx.principal.id
        if not is_self and not _can_administer_grants(ctx, service):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "forbidden", "missing_permission": Permission.ROLE_GRANT.value},
            )
        target_ctx = service.load_principal_context(ctx.workspace_id, target_id)
        if project_id is not None:
            resource = service.project_resource(ctx.workspace_id, project_id)
        else:
            resource = ResourceRef(workspace_id=ctx.workspace_id)
        eff = service.resolve(target_ctx, resource)
        return EffectiveAccessOut(
            principal=PrincipalRef(type=PrincipalType.USER, id=target_id),
            resource=resource,
            permissions=sorted(eff.permissions, key=lambda p: p.value),
            roles_by_scope={
                scope: sorted(roles, key=lambda r: r.value)
                for scope, roles in eff.roles_by_scope.items()
            },
            granting_sources=list(eff.granting_sources),
        )
