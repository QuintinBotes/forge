"""Project-access router (F30) — visibility + per-team access on a project.

Reads require ``project.read`` (invisible/cross-workspace -> 404, no existence
leak); visibility + team-access mutations require ``project.admin`` (enforced in
the service against the loaded project resource).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status

from forge_api.authz import map_authz_errors
from forge_api.authz.deps import AuthzServiceDep, PrincipalContextDep
from forge_api.deps import get_current_principal
from forge_api.schemas.authz import (
    ProjectAccessOut,
    ProjectTeamAccessIn,
    ProjectTeamAccessOut,
    ProjectVisibilityIn,
)
from forge_contracts.authz import Permission, ProjectVisibility

router = APIRouter(
    prefix="/projects",
    tags=["project-access"],
    dependencies=[Depends(get_current_principal)],
)


@router.get("/{project_id}/access", response_model=ProjectAccessOut)
def get_access(
    project_id: uuid.UUID, ctx: PrincipalContextDep, service: AuthzServiceDep
) -> ProjectAccessOut:
    with map_authz_errors():
        resource = service.project_resource(ctx.workspace_id, project_id)
        service._require(ctx, Permission.PROJECT_READ, resource)
        project = service.get_project(ctx.workspace_id, project_id)
        access = service.load_project_team_access(project_id)
        return ProjectAccessOut(
            project_id=project.id,
            visibility=ProjectVisibility(project.visibility),
            owner_team_id=project.owner_team_id,
            team_access=[
                ProjectTeamAccessOut(
                    project_id=a.project_id, team_id=a.team_id, access_level=a.access_level
                )
                for a in access
            ],
        )


@router.put("/{project_id}/visibility", response_model=ProjectAccessOut)
def set_visibility(
    project_id: uuid.UUID,
    body: ProjectVisibilityIn,
    ctx: PrincipalContextDep,
    service: AuthzServiceDep,
) -> ProjectAccessOut:
    with map_authz_errors():
        service.set_project_visibility(ctx, project_id, body.visibility, body.owner_team_id)
        service.session.commit()
        return get_access(project_id, ctx, service)


@router.get("/{project_id}/team-access", response_model=list[ProjectTeamAccessOut])
def list_team_access(
    project_id: uuid.UUID, ctx: PrincipalContextDep, service: AuthzServiceDep
) -> list[ProjectTeamAccessOut]:
    with map_authz_errors():
        resource = service.project_resource(ctx.workspace_id, project_id)
        service._require(ctx, Permission.PROJECT_READ, resource)
        return [
            ProjectTeamAccessOut(
                project_id=a.project_id, team_id=a.team_id, access_level=a.access_level
            )
            for a in service.load_project_team_access(project_id)
        ]


@router.post(
    "/{project_id}/team-access",
    response_model=ProjectTeamAccessOut,
    status_code=status.HTTP_201_CREATED,
)
def upsert_team_access(
    project_id: uuid.UUID,
    body: ProjectTeamAccessIn,
    ctx: PrincipalContextDep,
    service: AuthzServiceDep,
) -> ProjectTeamAccessOut:
    with map_authz_errors():
        row = service.upsert_project_team_access(ctx, project_id, body.team_id, body.access_level)
        service.session.commit()
        return ProjectTeamAccessOut(
            project_id=row.project_id, team_id=row.team_id, access_level=body.access_level
        )


@router.delete("/{project_id}/team-access/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_team_access(
    project_id: uuid.UUID,
    team_id: uuid.UUID,
    ctx: PrincipalContextDep,
    service: AuthzServiceDep,
) -> None:
    with map_authz_errors():
        service.remove_project_team_access(ctx, project_id, team_id)
        service.session.commit()
