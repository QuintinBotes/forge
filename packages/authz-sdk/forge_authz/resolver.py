"""The pure, total :class:`DefaultPermissionResolver` + RBAC invariants (F30 §4).

``resolve()`` is **pure** (no I/O), **total** (every input returns an
:class:`EffectiveAccess`, never raises), **deterministic** (same inputs ->
identical output), and **order-independent** over grants. It implements the
documented precedence rule: the effective permission set is the *union* of the
scope-narrowed permission sets of every applicable, unexpired grant, plus
team-derived project access, with the team-restricted visibility wall applied to
workspace grants (only a workspace admin bypasses it).

The escalation / lockout / team-cycle / team-depth invariants are pure functions
here too, so the single ``authz_service`` writer (and its unit tests) share one
implementation.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from uuid import UUID

from forge_authz.errors import (
    EscalationError,
    LastAdminError,
    TeamCycleError,
    TeamDepthError,
)
from forge_authz.permissions import (
    ACCESS_LEVEL_ROLE,
    ROLE_PERMISSIONS,
    ROLE_RANK,
    scope_narrow,
)
from forge_contracts.authz import (
    EffectiveAccess,
    Permission,
    PrincipalContext,
    ProjectTeamAccess,
    ResourceRef,
    Role,
    RoleGrant,
    ScopeType,
    TeamMembership,
    TeamRole,
)

__all__ = [
    "MAX_TEAM_DEPTH",
    "DefaultPermissionResolver",
    "check_grant_allowed",
    "ensure_not_last_admin",
    "team_closure",
    "validate_team_parent",
]

#: Maximum team nesting depth (inheritance + cycle/depth guards stop here).
MAX_TEAM_DEPTH = 5


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _is_live(grant: RoleGrant, now: datetime) -> bool:
    """A grant contributes only while unexpired (expiry is authoritative here)."""
    if grant.expires_at is None:
        return True
    expires = grant.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return expires > now


def team_closure(
    memberships: Iterable[TeamMembership], max_depth: int = MAX_TEAM_DEPTH
) -> set[UUID]:
    """The set of team ids a principal effectively belongs to (membership +
    ancestors via ``parent_team_id``), capped at ``max_depth`` levels.

    The walk uses the parent links present in ``memberships`` (the producer
    includes the ancestor closure for deep nesting) plus each membership's
    immediate ``parent_team_id`` so single-level nesting always inherits.
    """
    parent_of: dict[UUID, UUID | None] = {m.team_id: m.parent_team_id for m in memberships}
    teams: set[UUID] = set()
    for team_id, _ in list(parent_of.items()):
        current: UUID | None = team_id
        depth = 0
        while current is not None and depth <= max_depth:
            if current in teams:
                break
            teams.add(current)
            current = parent_of.get(current)
            depth += 1
    # Include each immediate parent even if it is not itself a membership.
    for parent in parent_of.values():
        if parent is not None:
            teams.add(parent)
    return teams


class DefaultPermissionResolver:
    """The reference :class:`~forge_contracts.authz.PermissionResolver`."""

    def resolve(
        self,
        principal: PrincipalContext,
        resource: ResourceRef,
        *,
        project_team_access: tuple[ProjectTeamAccess, ...] = (),
        now: datetime | None = None,
    ) -> EffectiveAccess:
        now = now or _utcnow()
        perms: set[Permission] = set()
        roles_by_scope: dict[ScopeType, set[Role]] = {}
        sources: set[str] = set()

        live_grants = [g for g in principal.grants if _is_live(g, now)]
        restricted = (
            resource.project_id is not None
            and resource.visibility is not None
            and resource.visibility.value == "team_restricted"
        )

        def _record(scope: ScopeType, role: Role) -> None:
            perms.update(scope_narrow(ROLE_PERMISSIONS[role], scope))
            roles_by_scope.setdefault(scope, set()).add(role)

        # 3. Workspace grants — gated by the team-restricted wall (admin bypass).
        for grant in live_grants:
            if grant.scope.type is not ScopeType.WORKSPACE:
                continue
            if grant.scope.id != resource.workspace_id:
                continue
            role = grant.role
            if restricted and role is not Role.ADMIN:
                continue  # walled off — only workspace admin sees a restricted project
            _record(ScopeType.WORKSPACE, role)
            sources.add(f"workspace:{role.value}")

        # 4. Project grants (direct project-scoped grants).
        if resource.project_id is not None:
            for grant in live_grants:
                if grant.scope.type is not ScopeType.PROJECT:
                    continue
                if grant.scope.id != resource.project_id:
                    continue
                _record(ScopeType.PROJECT, grant.role)
                sources.add(f"project:{grant.role.value}")

        # 5. Team-derived project access (membership + ancestor inheritance).
        if resource.project_id is not None:
            teams = team_closure(principal.team_memberships)
            for access in project_team_access:
                if access.project_id != resource.project_id:
                    continue
                if access.team_id not in teams:
                    continue
                _record(ScopeType.PROJECT, ACCESS_LEVEL_ROLE[access.access_level])
                sources.add(f"team {access.team_id}->{access.access_level.value}")

        # 5b. Team-lead confers team-member management on the lead's own team.
        # Resource-scoped: when ``resource.team_id`` is set the lead power applies
        # only to that team (so a lead of team A cannot manage team B); when no
        # team is targeted it is surfaced generally (inspector display).
        for membership in principal.team_memberships:
            if membership.team_role is not TeamRole.LEAD:
                continue
            if resource.team_id is not None and membership.team_id != resource.team_id:
                continue
            perms.add(Permission.TEAM_MEMBER_MANAGE)
            roles_by_scope.setdefault(ScopeType.TEAM, set()).add(Role.ADMIN)
            sources.add(f"team {membership.team_id}:lead")

        # 6. Team-scope grants that match an access row on this project.
        if resource.project_id is not None:
            for grant in live_grants:
                if grant.scope.type is not ScopeType.TEAM:
                    continue
                if not any(
                    a.team_id == grant.scope.id and a.project_id == resource.project_id
                    for a in project_team_access
                ):
                    continue
                _record(ScopeType.PROJECT, grant.role)
                sources.add(f"team-grant {grant.scope.id}:{grant.role.value}")

        return EffectiveAccess(
            permissions=frozenset(perms),
            roles_by_scope={scope: frozenset(roles) for scope, roles in roles_by_scope.items()},
            granting_sources=tuple(sorted(sources)),
        )

    def can(
        self,
        principal: PrincipalContext,
        permission: Permission,
        resource: ResourceRef,
        *,
        project_team_access: tuple[ProjectTeamAccess, ...] = (),
        now: datetime | None = None,
    ) -> bool:
        return self.resolve(
            principal, resource, project_team_access=project_team_access, now=now
        ).can(permission)


# --------------------------------------------------------------------------- #
# Pure invariants (escalation, lockout, team cycle/depth)                      #
# --------------------------------------------------------------------------- #


def max_role_at_scope(
    grants: Iterable[RoleGrant],
    scope_type: ScopeType,
    scope_id: UUID,
    *,
    now: datetime | None = None,
) -> Role | None:
    """The highest-ranked *live* role a principal holds at a given scope, if any."""
    now = now or _utcnow()
    best: Role | None = None
    best_rank = -1
    for grant in grants:
        if not _is_live(grant, now):
            continue
        if grant.scope.type is not scope_type or grant.scope.id != scope_id:
            continue
        rank = ROLE_RANK[grant.role]
        if rank > best_rank:
            best_rank = rank
            best = grant.role
    return best


def check_grant_allowed(
    *,
    actor_grants: Iterable[RoleGrant],
    actor_has_role_grant: bool,
    scope_type: ScopeType,
    scope_id: UUID,
    granted_role: Role,
    now: datetime | None = None,
) -> None:
    """Raise :class:`EscalationError` unless the actor may grant ``granted_role``.

    An actor may grant a role only if they (a) hold ``role.grant`` at the target
    scope and (b) hold a role at that scope whose rank is >= the granted role's.
    This makes self-escalation and granting above one's own level impossible.
    """
    actor_role = max_role_at_scope(actor_grants, scope_type, scope_id, now=now)
    if not actor_has_role_grant or actor_role is None:
        raise EscalationError(granted_role, actor_role)
    if ROLE_RANK[actor_role] < ROLE_RANK[granted_role]:
        raise EscalationError(granted_role, actor_role)


def ensure_not_last_admin(
    *,
    all_workspace_grants: Sequence[RoleGrant],
    workspace_id: UUID,
    removing_grant_id: UUID,
) -> None:
    """Raise :class:`LastAdminError` if removing the grant drops the last admin.

    ``all_workspace_grants`` is the full set of workspace-scope grants in the
    workspace; the check counts remaining workspace ``admin`` grants after the
    removal.
    """
    remaining_admins = [
        g
        for g in all_workspace_grants
        if g.id != removing_grant_id
        and g.scope.type is ScopeType.WORKSPACE
        and g.scope.id == workspace_id
        and g.role is Role.ADMIN
    ]
    if not remaining_admins:
        # Only a problem if the grant being removed *was* a workspace admin.
        removed = next((g for g in all_workspace_grants if g.id == removing_grant_id), None)
        if removed is not None and (
            removed.scope.type is ScopeType.WORKSPACE and removed.role is Role.ADMIN
        ):
            raise LastAdminError()


def validate_team_parent(
    *,
    team_id: UUID | None,
    parent_team_id: UUID | None,
    parents: dict[UUID, UUID | None],
    max_depth: int = MAX_TEAM_DEPTH,
) -> None:
    """Validate a team's proposed parent: no cycle, depth within ``max_depth``.

    ``parents`` maps existing ``team_id -> parent_team_id`` for the workspace.
    ``team_id`` is ``None`` when creating a brand-new team.
    """
    if parent_team_id is None:
        return
    # Walk up from the proposed parent. A cycle exists if we revisit team_id or
    # loop; depth is exceeded if the resulting chain length passes max_depth.
    chain: list[UUID] = []
    current: UUID | None = parent_team_id
    seen: set[UUID] = set()
    while current is not None:
        if current == team_id or current in seen:
            raise TeamCycleError([*chain, current])
        seen.add(current)
        chain.append(current)
        if len(chain) >= max_depth:
            raise TeamDepthError(max_depth)
        current = parents.get(current)
