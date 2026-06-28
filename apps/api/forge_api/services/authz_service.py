"""F30 authorization service — the single sanctioned writer of grants/teams/access.

Orchestrates the ``role_grant`` / ``team`` / ``team_member`` /
``project_team_access`` tables on a sync :class:`Session`, enforces the
escalation / lockout / team-cycle / team-depth invariants (delegating to the
pure ``forge_authz`` functions), resolves effective access via
``DefaultPermissionResolver``, and emits one immutable :class:`AuditEvent`
through an :class:`AuditSink` for every mutation (no path skips the audit).

Conforms to the in-tree foundation: sync SQLAlchemy ORM, singular table names,
``forge_api.deps.Principal`` for identity. Authorization is computed from
``role_grant`` only (the deprecated flat ``app_user.role`` is not read).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from forge_authz import (
    MAX_TEAM_DEPTH,
    ROLE_RANK,
    DefaultPermissionResolver,
    EscalationError,
    ensure_not_last_admin,
    validate_team_parent,
)
from forge_authz.errors import AccessDenied
from sqlalchemy import select
from sqlalchemy.orm import Session

from forge_api.services.audit import SqlAuditWriter
from forge_contracts.audit import AuditEvent
from forge_contracts.authz import (
    AccessLevel,
    EffectiveAccess,
    Permission,
    PrincipalContext,
    PrincipalRef,
    PrincipalType,
    ProjectVisibility,
    ResourceRef,
    Role,
    ScopeRef,
    ScopeType,
    TeamMembership,
    TeamRole,
)
from forge_contracts.authz import (
    ProjectTeamAccess as ProjectTeamAccessDTO,
)
from forge_contracts.authz import (
    RoleGrant as RoleGrantDTO,
)
from forge_db.models import (
    Project,
    ProjectTeamAccess,
    RoleGrant,
    Team,
    TeamMember,
)


class ResourceNotFound(Exception):
    """A referenced project/team/grant does not exist in the workspace (-> 404)."""


_RESOLVER = DefaultPermissionResolver()


def _grant_to_dto(row: RoleGrant) -> RoleGrantDTO:
    return RoleGrantDTO(
        id=row.id,
        workspace_id=row.workspace_id,
        principal=PrincipalRef(type=PrincipalType(row.principal_type), id=row.principal_id),
        scope=ScopeRef(type=ScopeType(row.scope_type), id=row.scope_id),
        role=Role(row.role),
        expires_at=row.expires_at,
    )


class AuthzService:
    """DB + audit orchestration for F30 authorization."""

    def __init__(self, session: Session, audit: SqlAuditWriter | None = None) -> None:
        self.session = session
        self.audit = audit or SqlAuditWriter(session)

    # -- context loading ---------------------------------------------------- #

    def load_principal_context(
        self, workspace_id: uuid.UUID, principal_id: uuid.UUID
    ) -> PrincipalContext:
        """Load all grants + team memberships (with ancestor closure) for a principal."""
        grants = self.session.scalars(
            select(RoleGrant).where(
                RoleGrant.workspace_id == workspace_id,
                RoleGrant.principal_id == principal_id,
            )
        ).all()
        teams_by_id = {
            t.id: t
            for t in self.session.scalars(
                select(Team).where(Team.workspace_id == workspace_id)
            ).all()
        }
        members = self.session.scalars(
            select(TeamMember).where(
                TeamMember.workspace_id == workspace_id,
                TeamMember.user_id == principal_id,
            )
        ).all()
        memberships: list[TeamMembership] = []
        seen: set[uuid.UUID] = set()
        for m in members:
            team = teams_by_id.get(m.team_id)
            parent = team.parent_team_id if team else None
            memberships.append(
                TeamMembership(
                    team_id=m.team_id, team_role=TeamRole(m.team_role), parent_team_id=parent
                )
            )
            seen.add(m.team_id)
            # Walk the ancestor chain so nested-team access inherits.
            cur = parent
            depth = 0
            while cur is not None and depth < MAX_TEAM_DEPTH:
                if cur in seen:
                    break
                seen.add(cur)
                ancestor = teams_by_id.get(cur)
                memberships.append(
                    TeamMembership(
                        team_id=cur,
                        team_role=TeamRole.MEMBER,
                        parent_team_id=ancestor.parent_team_id if ancestor else None,
                    )
                )
                cur = ancestor.parent_team_id if ancestor else None
                depth += 1

        principal_type = PrincipalType(grants[0].principal_type) if grants else PrincipalType.USER
        return PrincipalContext(
            principal=PrincipalRef(type=principal_type, id=principal_id),
            workspace_id=workspace_id,
            grants=tuple(_grant_to_dto(g) for g in grants),
            team_memberships=tuple(memberships),
        )

    def load_project_team_access(self, project_id: uuid.UUID) -> tuple[ProjectTeamAccessDTO, ...]:
        rows = self.session.scalars(
            select(ProjectTeamAccess).where(ProjectTeamAccess.project_id == project_id)
        ).all()
        return tuple(
            ProjectTeamAccessDTO(
                project_id=r.project_id, team_id=r.team_id, access_level=AccessLevel(r.access_level)
            )
            for r in rows
        )

    def _project(self, workspace_id: uuid.UUID, project_id: uuid.UUID) -> Project:
        project = self.session.get(Project, project_id)
        if project is None or project.workspace_id != workspace_id:
            raise ResourceNotFound("project not found")
        return project

    def _team(self, workspace_id: uuid.UUID, team_id: uuid.UUID) -> Team:
        team = self.session.get(Team, team_id)
        if team is None or team.workspace_id != workspace_id:
            raise ResourceNotFound("team not found")
        return team

    def get_project(self, workspace_id: uuid.UUID, project_id: uuid.UUID) -> Project:
        return self._project(workspace_id, project_id)

    def project_resource(self, workspace_id: uuid.UUID, project_id: uuid.UUID) -> ResourceRef:
        project = self._project(workspace_id, project_id)
        return ResourceRef(
            workspace_id=workspace_id,
            project_id=project.id,
            team_id=project.owner_team_id,
            visibility=ProjectVisibility(project.visibility),
        )

    # -- resolution / introspection ---------------------------------------- #

    def resolve(self, ctx: PrincipalContext, resource: ResourceRef) -> EffectiveAccess:
        pta = (
            self.load_project_team_access(resource.project_id)
            if resource.project_id is not None
            else ()
        )
        return _RESOLVER.resolve(ctx, resource, project_team_access=pta)

    def effective_access(self, ctx: PrincipalContext, resource: ResourceRef) -> EffectiveAccess:
        return self.resolve(ctx, resource)

    def _require(
        self,
        ctx: PrincipalContext,
        permission: Permission,
        resource: ResourceRef,
    ) -> EffectiveAccess:
        """Authorize ``permission`` on ``resource`` or raise.

        Raises :class:`ResourceNotFound` (-> 404, no existence leak) when the
        principal cannot even read a project resource; otherwise
        :class:`AccessDenied` (-> 403) for the specific permission.
        """
        eff = self.resolve(ctx, resource)
        if permission in eff.permissions:
            return eff
        if resource.project_id is not None and Permission.PROJECT_READ not in eff.permissions:
            raise ResourceNotFound("project not found")
        raise AccessDenied(permission, resource_scope_type(resource), resource_scope_id(resource))

    # -- grants ------------------------------------------------------------- #

    def list_grants(
        self,
        workspace_id: uuid.UUID,
        *,
        principal_id: uuid.UUID | None = None,
        scope_type: ScopeType | None = None,
        scope_id: uuid.UUID | None = None,
    ) -> list[RoleGrantDTO]:
        stmt = select(RoleGrant).where(RoleGrant.workspace_id == workspace_id)
        if principal_id is not None:
            stmt = stmt.where(RoleGrant.principal_id == principal_id)
        if scope_type is not None:
            stmt = stmt.where(RoleGrant.scope_type == scope_type)
        if scope_id is not None:
            stmt = stmt.where(RoleGrant.scope_id == scope_id)
        return [_grant_to_dto(g) for g in self.session.scalars(stmt).all()]

    def _scope_resource(self, workspace_id: uuid.UUID, scope: ScopeRef) -> ResourceRef:
        if scope.type is ScopeType.PROJECT:
            return self.project_resource(workspace_id, scope.id)
        if scope.type is ScopeType.TEAM:
            self._team(workspace_id, scope.id)
            return ResourceRef(workspace_id=workspace_id, team_id=scope.id)
        return ResourceRef(workspace_id=workspace_id)

    def _assert_can_grant(self, actor_eff: EffectiveAccess, granted_role: Role) -> None:
        if Permission.ROLE_GRANT not in actor_eff.permissions:
            raise EscalationError(granted_role, _max_role(actor_eff))
        actor_rank = max(
            (ROLE_RANK[r] for roles in actor_eff.roles_by_scope.values() for r in roles),
            default=-1,
        )
        if actor_rank < ROLE_RANK[granted_role]:
            raise EscalationError(granted_role, _max_role(actor_eff))

    def grant_role(
        self,
        actor: PrincipalContext,
        principal: PrincipalRef,
        scope: ScopeRef,
        role: Role,
        *,
        expires_at: datetime | None = None,
    ) -> RoleGrantDTO:
        workspace_id = actor.workspace_id
        # Workspace-scope grants must target the workspace itself.
        scope_id = workspace_id if scope.type is ScopeType.WORKSPACE else scope.id
        scope = ScopeRef(type=scope.type, id=scope_id)
        resource = self._scope_resource(workspace_id, scope)
        actor_eff = self.resolve(actor, resource)
        self._assert_can_grant(actor_eff, role)

        existing = self.session.scalar(
            select(RoleGrant).where(
                RoleGrant.workspace_id == workspace_id,
                RoleGrant.principal_type == principal.type,
                RoleGrant.principal_id == principal.id,
                RoleGrant.scope_type == scope.type,
                RoleGrant.scope_id == scope.id,
                RoleGrant.role == role,
            )
        )
        if existing is not None:
            before = {"expires_at": _iso(existing.expires_at)}
            existing.expires_at = expires_at
            self.session.flush()
            self._emit(
                actor,
                "role_grant.created",
                target_type="role_grant",
                target_id=existing.id,
                scope=scope,
                before=before,
                after=_grant_audit(existing),
                principal_target=principal,
            )
            return _grant_to_dto(existing)

        row = RoleGrant(
            workspace_id=workspace_id,
            principal_type=principal.type,
            principal_id=principal.id,
            scope_type=scope.type,
            scope_id=scope.id,
            role=role,
            granted_by=actor.principal.id,
            expires_at=expires_at,
        )
        self.session.add(row)
        self.session.flush()
        self._emit(
            actor,
            "role_grant.created",
            target_type="role_grant",
            target_id=row.id,
            scope=scope,
            after=_grant_audit(row),
            principal_target=principal,
        )
        return _grant_to_dto(row)

    def revoke_role(self, actor: PrincipalContext, grant_id: uuid.UUID) -> None:
        row = self.session.get(RoleGrant, grant_id)
        if row is None or row.workspace_id != actor.workspace_id:
            raise ResourceNotFound("grant not found")
        scope = ScopeRef(type=ScopeType(row.scope_type), id=row.scope_id)
        resource = self._scope_resource(actor.workspace_id, scope)
        actor_eff = self.resolve(actor, resource)
        if Permission.ROLE_GRANT not in actor_eff.permissions:
            raise EscalationError(Role(row.role), _max_role(actor_eff))

        # Lockout: do not remove the last workspace admin.
        all_ws_grants = self.session.scalars(
            select(RoleGrant).where(
                RoleGrant.workspace_id == actor.workspace_id,
                RoleGrant.scope_type == ScopeType.WORKSPACE,
            )
        ).all()
        ensure_not_last_admin(
            all_workspace_grants=[_grant_to_dto(g) for g in all_ws_grants],
            workspace_id=actor.workspace_id,
            removing_grant_id=grant_id,
        )

        before = _grant_audit(row)
        self.session.delete(row)
        self.session.flush()
        self._emit(
            actor,
            "role_grant.revoked",
            target_type="role_grant",
            target_id=grant_id,
            scope=scope,
            before=before,
        )

    # -- teams -------------------------------------------------------------- #

    def _team_parents(self, workspace_id: uuid.UUID) -> dict[uuid.UUID, uuid.UUID | None]:
        return {
            t.id: t.parent_team_id
            for t in self.session.scalars(
                select(Team).where(Team.workspace_id == workspace_id)
            ).all()
        }

    def list_teams(self, workspace_id: uuid.UUID) -> list[Team]:
        return list(
            self.session.scalars(select(Team).where(Team.workspace_id == workspace_id)).all()
        )

    def get_team(self, workspace_id: uuid.UUID, team_id: uuid.UUID) -> Team:
        return self._team(workspace_id, team_id)

    def create_team(
        self,
        actor: PrincipalContext,
        *,
        key: str,
        name: str,
        description: str | None = None,
        parent_team_id: uuid.UUID | None = None,
    ) -> Team:
        self._require(actor, Permission.TEAM_MANAGE, ResourceRef(workspace_id=actor.workspace_id))
        if parent_team_id is not None:
            self._team(actor.workspace_id, parent_team_id)
        validate_team_parent(
            team_id=None,
            parent_team_id=parent_team_id,
            parents=self._team_parents(actor.workspace_id),
        )
        row = Team(
            workspace_id=actor.workspace_id,
            key=key,
            name=name,
            description=description,
            parent_team_id=parent_team_id,
            created_by=actor.principal.id,
        )
        self.session.add(row)
        self.session.flush()
        self._emit(
            actor,
            "team.created",
            target_type="team",
            target_id=row.id,
            after={"key": key, "name": name, "parent_team_id": _str(parent_team_id)},
        )
        return row

    def update_team(
        self,
        actor: PrincipalContext,
        team_id: uuid.UUID,
        *,
        name: str | None = None,
        description: str | None = None,
        parent_team_id: uuid.UUID | None = None,
        update_parent: bool = False,
    ) -> Team:
        self._require(actor, Permission.TEAM_MANAGE, ResourceRef(workspace_id=actor.workspace_id))
        team = self._team(actor.workspace_id, team_id)
        before = {"name": team.name, "parent_team_id": _str(team.parent_team_id)}
        if name is not None:
            team.name = name
        if description is not None:
            team.description = description
        if update_parent:
            if parent_team_id is not None:
                self._team(actor.workspace_id, parent_team_id)
            validate_team_parent(
                team_id=team_id,
                parent_team_id=parent_team_id,
                parents=self._team_parents(actor.workspace_id),
            )
            team.parent_team_id = parent_team_id
        self.session.flush()
        self._emit(
            actor,
            "team.updated",
            target_type="team",
            target_id=team.id,
            before=before,
            after={"name": team.name, "parent_team_id": _str(team.parent_team_id)},
        )
        return team

    def archive_team(self, actor: PrincipalContext, team_id: uuid.UUID) -> Team:
        self._require(actor, Permission.TEAM_MANAGE, ResourceRef(workspace_id=actor.workspace_id))
        team = self._team(actor.workspace_id, team_id)
        team.archived_at = _utcnow()
        self.session.flush()
        self._emit(actor, "team.archived", target_type="team", target_id=team.id)
        return team

    # -- team membership ---------------------------------------------------- #

    def list_team_members(self, workspace_id: uuid.UUID, team_id: uuid.UUID) -> list[TeamMember]:
        self._team(workspace_id, team_id)
        return list(
            self.session.scalars(select(TeamMember).where(TeamMember.team_id == team_id)).all()
        )

    def _require_team_member_manage(self, actor: PrincipalContext, team_id: uuid.UUID) -> None:
        self._team(actor.workspace_id, team_id)
        self._require(
            actor,
            Permission.TEAM_MEMBER_MANAGE,
            ResourceRef(workspace_id=actor.workspace_id, team_id=team_id),
        )

    def add_team_member(
        self,
        actor: PrincipalContext,
        team_id: uuid.UUID,
        user_id: uuid.UUID,
        team_role: TeamRole = TeamRole.MEMBER,
    ) -> TeamMember:
        self._require_team_member_manage(actor, team_id)
        existing = self.session.scalar(
            select(TeamMember).where(TeamMember.team_id == team_id, TeamMember.user_id == user_id)
        )
        member_after = {
            "team_id": _str(team_id),
            "user_id": _str(user_id),
            "team_role": team_role.value,
        }
        if existing is not None:
            existing.team_role = team_role
            self.session.flush()
            self._emit(
                actor,
                "team_member.role_changed",
                target_type="team_member",
                target_id=existing.id,
                after=member_after,
            )
            return existing
        row = TeamMember(
            workspace_id=actor.workspace_id,
            team_id=team_id,
            user_id=user_id,
            team_role=team_role,
        )
        self.session.add(row)
        self.session.flush()
        self._emit(
            actor,
            "team_member.added",
            target_type="team_member",
            target_id=row.id,
            after=member_after,
        )
        return row

    def set_team_role(
        self,
        actor: PrincipalContext,
        team_id: uuid.UUID,
        user_id: uuid.UUID,
        team_role: TeamRole,
    ) -> TeamMember:
        self._require_team_member_manage(actor, team_id)
        row = self.session.scalar(
            select(TeamMember).where(TeamMember.team_id == team_id, TeamMember.user_id == user_id)
        )
        if row is None:
            raise ResourceNotFound("team member not found")
        before = {"team_role": TeamRole(row.team_role).value}
        row.team_role = team_role
        self.session.flush()
        self._emit(
            actor,
            "team_member.role_changed",
            target_type="team_member",
            target_id=row.id,
            before=before,
            after={"team_role": team_role.value},
        )
        return row

    def remove_team_member(
        self, actor: PrincipalContext, team_id: uuid.UUID, user_id: uuid.UUID
    ) -> None:
        self._require_team_member_manage(actor, team_id)
        row = self.session.scalar(
            select(TeamMember).where(TeamMember.team_id == team_id, TeamMember.user_id == user_id)
        )
        if row is None:
            raise ResourceNotFound("team member not found")
        member_id = row.id
        self.session.delete(row)
        self.session.flush()
        self._emit(
            actor,
            "team_member.removed",
            target_type="team_member",
            target_id=member_id,
            before={"team_id": _str(team_id), "user_id": _str(user_id)},
        )

    # -- project access ----------------------------------------------------- #

    def set_project_visibility(
        self,
        actor: PrincipalContext,
        project_id: uuid.UUID,
        visibility: ProjectVisibility,
        owner_team_id: uuid.UUID | None = None,
    ) -> Project:
        resource = self.project_resource(actor.workspace_id, project_id)
        self._require(actor, Permission.PROJECT_ADMIN, resource)
        project = self._project(actor.workspace_id, project_id)
        before = {"visibility": project.visibility, "owner_team_id": _str(project.owner_team_id)}
        project.visibility = visibility
        if owner_team_id is not None:
            self._team(actor.workspace_id, owner_team_id)
            project.owner_team_id = owner_team_id
        self.session.flush()
        self._emit(
            actor,
            "project_access.visibility_changed",
            target_type="project",
            target_id=project.id,
            before=before,
            after={"visibility": visibility.value, "owner_team_id": _str(project.owner_team_id)},
        )
        return project

    def upsert_project_team_access(
        self,
        actor: PrincipalContext,
        project_id: uuid.UUID,
        team_id: uuid.UUID,
        access_level: AccessLevel,
    ) -> ProjectTeamAccess:
        resource = self.project_resource(actor.workspace_id, project_id)
        self._require(actor, Permission.PROJECT_ADMIN, resource)
        self._team(actor.workspace_id, team_id)
        row = self.session.scalar(
            select(ProjectTeamAccess).where(
                ProjectTeamAccess.project_id == project_id,
                ProjectTeamAccess.team_id == team_id,
            )
        )
        if row is None:
            row = ProjectTeamAccess(
                workspace_id=actor.workspace_id,
                project_id=project_id,
                team_id=team_id,
                access_level=access_level,
            )
            self.session.add(row)
        else:
            row.access_level = access_level
        self.session.flush()
        self._emit(
            actor,
            "project_access.team_access_set",
            target_type="project",
            target_id=project_id,
            after={"team_id": _str(team_id), "access_level": access_level.value},
        )
        return row

    def remove_project_team_access(
        self, actor: PrincipalContext, project_id: uuid.UUID, team_id: uuid.UUID
    ) -> None:
        resource = self.project_resource(actor.workspace_id, project_id)
        self._require(actor, Permission.PROJECT_ADMIN, resource)
        row = self.session.scalar(
            select(ProjectTeamAccess).where(
                ProjectTeamAccess.project_id == project_id,
                ProjectTeamAccess.team_id == team_id,
            )
        )
        if row is None:
            raise ResourceNotFound("team access not found")
        self.session.delete(row)
        self.session.flush()
        self._emit(
            actor,
            "project_access.team_access_removed",
            target_type="project",
            target_id=project_id,
            before={"team_id": _str(team_id)},
        )

    # -- audit -------------------------------------------------------------- #

    def _emit(
        self,
        actor: PrincipalContext,
        action: str,
        *,
        target_type: str,
        target_id: uuid.UUID,
        scope: ScopeRef | None = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        principal_target: PrincipalRef | None = None,
    ) -> None:
        details: dict[str, Any] = {}
        if principal_target is not None:
            details["target_principal"] = {
                "type": principal_target.type.value,
                "id": str(principal_target.id),
            }
        self.audit.emit(
            AuditEvent(
                workspace_id=actor.workspace_id,
                action=action,
                actor_id=actor.principal.id,
                actor_type=actor.principal.type.value,
                target_type=target_type,
                target_id=target_id,
                scope_type=scope.type.value if scope else None,
                scope_id=scope.id if scope else None,
                before=before,
                after=after,
                details=details,
            )
        )


def _max_role(eff: EffectiveAccess) -> Role | None:
    best: Role | None = None
    best_rank = -1
    for roles in eff.roles_by_scope.values():
        for r in roles:
            if ROLE_RANK[r] > best_rank:
                best_rank = ROLE_RANK[r]
                best = r
    return best


def _grant_audit(row: RoleGrant) -> dict[str, Any]:
    return {
        "principal_type": str(row.principal_type),
        "principal_id": str(row.principal_id),
        "scope_type": str(row.scope_type),
        "scope_id": str(row.scope_id),
        "role": str(row.role),
        "expires_at": _iso(row.expires_at),
    }


def resource_scope_type(resource: ResourceRef) -> ScopeType:
    if resource.project_id is not None:
        return ScopeType.PROJECT
    if resource.team_id is not None:
        return ScopeType.TEAM
    return ScopeType.WORKSPACE


def resource_scope_id(resource: ResourceRef) -> uuid.UUID:
    if resource.project_id is not None:
        return resource.project_id
    if resource.team_id is not None:
        return resource.team_id
    return resource.workspace_id


def _str(value: uuid.UUID | None) -> str | None:
    return str(value) if value is not None else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _utcnow() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)


__all__ = ["AuthzService", "ResourceNotFound"]
