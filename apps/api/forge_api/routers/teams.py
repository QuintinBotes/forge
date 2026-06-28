"""Teams router (F30) — team CRUD + membership management.

All routes authenticate (401 if anonymous). Authorization is enforced in the
:class:`AuthzService` against the loaded resource (``team.manage`` for team CRUD;
``team.member.manage`` — workspace admin or team lead — for membership), so the
team-lead scope (manage own team, 403 on others) and cross-workspace 404 are
handled there.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status

from forge_api.authz import map_authz_errors
from forge_api.authz.deps import AuthzServiceDep, PrincipalContextDep
from forge_api.deps import get_current_principal
from forge_api.schemas.authz import (
    TeamIn,
    TeamMemberIn,
    TeamMemberOut,
    TeamMemberRoleIn,
    TeamOut,
    TeamUpdate,
)
from forge_contracts.authz import TeamRole
from forge_db.models import Team, TeamMember

router = APIRouter(
    prefix="/teams",
    tags=["teams"],
    dependencies=[Depends(get_current_principal)],
)


def _team_out(t: Team) -> TeamOut:
    return TeamOut(
        id=t.id,
        key=t.key,
        name=t.name,
        description=t.description,
        parent_team_id=t.parent_team_id,
        archived_at=t.archived_at,
        created_at=t.created_at,
    )


def _member_out(m: TeamMember) -> TeamMemberOut:
    return TeamMemberOut(
        user_id=m.user_id, team_role=TeamRole(m.team_role), created_at=m.created_at
    )


@router.get("", response_model=list[TeamOut])
def list_teams(ctx: PrincipalContextDep, service: AuthzServiceDep) -> list[TeamOut]:
    return [_team_out(t) for t in service.list_teams(ctx.workspace_id)]


@router.post("", response_model=TeamOut, status_code=status.HTTP_201_CREATED)
def create_team(body: TeamIn, ctx: PrincipalContextDep, service: AuthzServiceDep) -> TeamOut:
    with map_authz_errors():
        team = service.create_team(
            ctx,
            key=body.key,
            name=body.name,
            description=body.description,
            parent_team_id=body.parent_team_id,
        )
        service.session.commit()
        return _team_out(team)


@router.get("/{team_id}", response_model=TeamOut)
def get_team(team_id: uuid.UUID, ctx: PrincipalContextDep, service: AuthzServiceDep) -> TeamOut:
    with map_authz_errors():
        return _team_out(service.get_team(ctx.workspace_id, team_id))


@router.patch("/{team_id}", response_model=TeamOut)
def update_team(
    team_id: uuid.UUID,
    body: TeamUpdate,
    ctx: PrincipalContextDep,
    service: AuthzServiceDep,
) -> TeamOut:
    with map_authz_errors():
        team = service.update_team(
            ctx,
            team_id,
            name=body.name,
            description=body.description,
            parent_team_id=body.parent_team_id,
            update_parent="parent_team_id" in body.model_fields_set,
        )
        service.session.commit()
        return _team_out(team)


@router.post("/{team_id}/archive", response_model=TeamOut)
def archive_team(team_id: uuid.UUID, ctx: PrincipalContextDep, service: AuthzServiceDep) -> TeamOut:
    with map_authz_errors():
        team = service.archive_team(ctx, team_id)
        service.session.commit()
        return _team_out(team)


@router.get("/{team_id}/members", response_model=list[TeamMemberOut])
def list_members(
    team_id: uuid.UUID, ctx: PrincipalContextDep, service: AuthzServiceDep
) -> list[TeamMemberOut]:
    with map_authz_errors():
        return [_member_out(m) for m in service.list_team_members(ctx.workspace_id, team_id)]


@router.post(
    "/{team_id}/members", response_model=TeamMemberOut, status_code=status.HTTP_201_CREATED
)
def add_member(
    team_id: uuid.UUID,
    body: TeamMemberIn,
    ctx: PrincipalContextDep,
    service: AuthzServiceDep,
) -> TeamMemberOut:
    with map_authz_errors():
        member = service.add_team_member(ctx, team_id, body.user_id, body.team_role)
        service.session.commit()
        return _member_out(member)


@router.patch("/{team_id}/members/{user_id}", response_model=TeamMemberOut)
def set_member_role(
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    body: TeamMemberRoleIn,
    ctx: PrincipalContextDep,
    service: AuthzServiceDep,
) -> TeamMemberOut:
    with map_authz_errors():
        member = service.set_team_role(ctx, team_id, user_id, body.team_role)
        service.session.commit()
        return _member_out(member)


@router.delete("/{team_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    ctx: PrincipalContextDep,
    service: AuthzServiceDep,
) -> None:
    with map_authz_errors():
        service.remove_team_member(ctx, team_id, user_id)
        service.session.commit()
