"""Frozen authorization DTOs + the ``PermissionResolver`` Protocol (F30).

F30 (multi-team workspace controls & full RBAC) upgrades the v1 *flat*
per-workspace role model into a **scoped, hierarchical** model: a principal
(user / API key / service identity) holds role grants at workspace / team /
project scope, and a pure :class:`PermissionResolver` computes the principal's
effective permission set on any resource.

This module is the **frozen contract** shared by ``forge_authz`` (the pure
resolver SDK), ``forge_db`` (the ORM + migration), and ``forge_api`` (the
service/router/deps layer) so they all speak one object set.

Foundation deviations (the in-tree foundation differs from the idealized
multi-team-RBAC design in ``docs/FORGE_SPEC.md``):

* The idealized doc reuses ``forge_contracts.auth.PrincipalType`` from the F37
  auth foundation. That module does not exist in-tree, so :class:`PrincipalType`
  is defined here.
* The flat four-role enum is reused verbatim as :data:`Role` (an alias of
  :class:`forge_contracts.enums.UserRole`) so the ``agent-runner`` wire value
  (HYPHEN) and the v1 backfill equivalence are preserved.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict

# Reuse the foundation's flat role enum VERBATIM (do not redefine — that would
# drift the ``agent-runner`` value and break backfill equivalence, AC1).
from forge_contracts.enums import UserRole as Role

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


class PrincipalType(StrEnum):
    """The kind of identity a grant binds to.

    Foundation note: the idealized slice reuses ``forge_contracts.auth`` which is
    absent in-tree, so the type is defined here.
    """

    USER = "user"
    API_KEY = "api_key"
    SERVICE = "service"


class ScopeType(StrEnum):
    """The scope at which a role grant applies."""

    WORKSPACE = "workspace"
    TEAM = "team"
    PROJECT = "project"


class TeamRole(StrEnum):
    """A member's role within a team (``lead`` confers team-member management)."""

    LEAD = "lead"
    MEMBER = "member"


class AccessLevel(StrEnum):
    """A team's access level on a project (maps to a :class:`Role`)."""

    READ = "read"
    WRITE = "write"
    ADMIN = "admin"


class ProjectVisibility(StrEnum):
    """Whether a project is visible workspace-wide or walled off to teams."""

    WORKSPACE = "workspace"
    TEAM_RESTRICTED = "team_restricted"


class Permission(StrEnum):
    """The fine-grained capabilities the resolver evaluates."""

    WORKSPACE_ADMIN = "workspace.admin"
    MEMBER_MANAGE = "member.manage"
    TEAM_MANAGE = "team.manage"
    TEAM_MEMBER_MANAGE = "team.member.manage"
    ROLE_GRANT = "role.grant"
    PROJECT_CREATE = "project.create"
    PROJECT_READ = "project.read"
    PROJECT_WRITE = "project.write"
    PROJECT_ADMIN = "project.admin"
    PROJECT_DELETE = "project.delete"
    TASK_READ = "task.read"
    TASK_WRITE = "task.write"
    SPEC_APPROVE = "spec.approve"
    PR_APPROVE = "pr.approve"
    DEPLOY_APPROVE = "deploy.approve"
    AGENT_RUN = "agent.run"
    AUDIT_READ = "audit.read"
    KNOWLEDGE_MANAGE = "knowledge.manage"
    MCP_MANAGE = "mcp.manage"
    POLICY_MANAGE = "policy.manage"
    INTEGRATION_MANAGE = "integration.manage"
    SECRETS_MANAGE = "secrets.manage"


class ScopeRef(BaseModel):
    """A reference to a scope (workspace / team / project) by id."""

    model_config = ConfigDict(frozen=True)
    type: ScopeType
    id: UUID


class PrincipalRef(BaseModel):
    """A reference to a principal (user / api_key / service) by id."""

    model_config = ConfigDict(frozen=True)
    type: PrincipalType
    id: UUID


class RoleGrant(BaseModel):
    """A single ``(principal, scope, role)`` grant, optionally time-bounded."""

    model_config = ConfigDict(frozen=True)
    id: UUID
    workspace_id: UUID
    principal: PrincipalRef
    scope: ScopeRef
    role: Role
    expires_at: datetime | None = None


class TeamMembership(BaseModel):
    """A principal's membership in a team (carries the immediate parent link)."""

    model_config = ConfigDict(frozen=True)
    team_id: UUID
    team_role: TeamRole
    parent_team_id: UUID | None = None


class ProjectTeamAccess(BaseModel):
    """A team's access level on a project."""

    model_config = ConfigDict(frozen=True)
    project_id: UUID
    team_id: UUID
    access_level: AccessLevel


class ResourceRef(BaseModel):
    """The resource an access check targets."""

    model_config = ConfigDict(frozen=True)
    workspace_id: UUID
    project_id: UUID | None = None
    team_id: UUID | None = None
    visibility: ProjectVisibility = ProjectVisibility.WORKSPACE


class PrincipalContext(BaseModel):
    """All grants + team memberships for a principal in one workspace.

    Built once per request (batched DB load) and fed to the resolver. For nested
    teams the producer should include the full ancestor closure so the resolver
    can inherit project-team access up the tree (capped at ``MAX_TEAM_DEPTH``).
    """

    model_config = ConfigDict(frozen=True)
    principal: PrincipalRef
    workspace_id: UUID
    grants: tuple[RoleGrant, ...]
    team_memberships: tuple[TeamMembership, ...]


class EffectiveAccess(BaseModel):
    """The computed permission set for a principal on a resource."""

    model_config = ConfigDict(frozen=True)
    permissions: frozenset[Permission]
    roles_by_scope: dict[ScopeType, frozenset[Role]]
    granting_sources: tuple[str, ...]

    def can(self, permission: Permission) -> bool:
        """True iff ``permission`` is in the effective set."""
        return permission in self.permissions


@runtime_checkable
class PermissionResolver(Protocol):
    """Pure, total computation of effective access (no I/O)."""

    def resolve(
        self,
        principal: PrincipalContext,
        resource: ResourceRef,
        *,
        project_team_access: tuple[ProjectTeamAccess, ...] = (),
        now: datetime | None = None,
    ) -> EffectiveAccess: ...

    def can(
        self,
        principal: PrincipalContext,
        permission: Permission,
        resource: ResourceRef,
        *,
        project_team_access: tuple[ProjectTeamAccess, ...] = (),
        now: datetime | None = None,
    ) -> bool: ...
