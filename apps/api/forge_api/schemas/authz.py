"""REST request/response models for the F30 authz surface.

These wrap the frozen ``forge_contracts.authz`` value objects (``PrincipalRef`` /
``ScopeRef`` / enums) for the teams / access / project-access routers.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from forge_contracts.authz import (
    AccessLevel,
    Permission,
    PrincipalRef,
    ProjectVisibility,
    ResourceRef,
    Role,
    ScopeRef,
    ScopeType,
    TeamRole,
)


class TeamIn(BaseModel):
    key: str
    name: str
    description: str | None = None
    parent_team_id: UUID | None = None


class TeamUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    parent_team_id: UUID | None = None


class TeamOut(BaseModel):
    id: UUID
    key: str
    name: str
    description: str | None = None
    parent_team_id: UUID | None = None
    archived_at: datetime | None = None
    created_at: datetime


class TeamMemberIn(BaseModel):
    user_id: UUID
    team_role: TeamRole = TeamRole.MEMBER


class TeamMemberRoleIn(BaseModel):
    team_role: TeamRole


class TeamMemberOut(BaseModel):
    user_id: UUID
    team_role: TeamRole
    created_at: datetime


class RoleGrantIn(BaseModel):
    principal: PrincipalRef
    scope: ScopeRef
    role: Role
    expires_at: datetime | None = None


class RoleGrantOut(BaseModel):
    id: UUID
    workspace_id: UUID
    principal: PrincipalRef
    scope: ScopeRef
    role: Role
    granted_by: UUID | None = None
    expires_at: datetime | None = None
    created_at: datetime | None = None


class ProjectVisibilityIn(BaseModel):
    visibility: ProjectVisibility
    owner_team_id: UUID | None = None


class ProjectTeamAccessIn(BaseModel):
    team_id: UUID
    access_level: AccessLevel


class ProjectTeamAccessOut(BaseModel):
    project_id: UUID
    team_id: UUID
    access_level: AccessLevel


class ProjectAccessOut(BaseModel):
    project_id: UUID
    visibility: ProjectVisibility
    owner_team_id: UUID | None = None
    team_access: list[ProjectTeamAccessOut]


class EffectiveAccessOut(BaseModel):
    principal: PrincipalRef
    resource: ResourceRef
    permissions: list[Permission]
    roles_by_scope: dict[ScopeType, list[Role]]
    granting_sources: list[str]
