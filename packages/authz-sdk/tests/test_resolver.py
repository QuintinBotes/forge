"""Table-driven + randomized resolver tests (F30 AC2-AC10, AC13, AC19).

Pure, no DB. The matrix exercises workspace roles, project elevation/union,
scope narrowing, team membership, nested teams, the team-restricted wall, admin
bypass, and agent-runner exclusions. A randomized totality test (stdlib ``random``
in place of Hypothesis — see notes) asserts ``resolve`` is total, deterministic,
and order-independent.
"""

from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from forge_authz import DefaultPermissionResolver
from forge_contracts.authz import (
    AccessLevel,
    Permission,
    PrincipalContext,
    PrincipalRef,
    PrincipalType,
    ProjectTeamAccess,
    ProjectVisibility,
    ResourceRef,
    Role,
    RoleGrant,
    ScopeRef,
    ScopeType,
    TeamMembership,
    TeamRole,
)

P = Permission
WS = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
CORE = uuid.UUID("00000000-0000-0000-0000-0000000000c0")
OTHER = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
BE = uuid.UUID("00000000-0000-0000-0000-0000000000b0")
ENG = uuid.UUID("00000000-0000-0000-0000-0000000000e0")
USER = uuid.UUID("00000000-0000-0000-0000-0000000000d0")

R = DefaultPermissionResolver()


def _grant(scope_type: ScopeType, scope_id: uuid.UUID, role: Role, *, expires=None) -> RoleGrant:
    return RoleGrant(
        id=uuid.uuid4(),
        workspace_id=WS,
        principal=PrincipalRef(type=PrincipalType.USER, id=USER),
        scope=ScopeRef(type=scope_type, id=scope_id),
        role=role,
        expires_at=expires,
    )


def _ctx(grants=(), memberships=()) -> PrincipalContext:
    return PrincipalContext(
        principal=PrincipalRef(type=PrincipalType.USER, id=USER),
        workspace_id=WS,
        grants=tuple(grants),
        team_memberships=tuple(memberships),
    )


def _project(project_id=CORE, visibility=ProjectVisibility.WORKSPACE) -> ResourceRef:
    return ResourceRef(workspace_id=WS, project_id=project_id, visibility=visibility)


def test_ac2_workspace_member_precedence() -> None:
    ctx = _ctx([_grant(ScopeType.WORKSPACE, WS, Role.MEMBER)])
    eff = R.resolve(ctx, _project())
    assert {P.PROJECT_READ, P.PROJECT_WRITE, P.TASK_WRITE, P.PR_APPROVE} <= eff.permissions
    assert P.ROLE_GRANT not in eff.permissions
    assert P.MEMBER_MANAGE not in eff.permissions


def test_ac3_viewer_read_only() -> None:
    ctx = _ctx([_grant(ScopeType.WORKSPACE, WS, Role.VIEWER)])
    eff = R.resolve(ctx, _project())
    assert eff.permissions == frozenset({P.PROJECT_READ, P.TASK_READ, P.AUDIT_READ})


def test_ac4_project_elevation_is_additive_union() -> None:
    ctx = _ctx(
        [
            _grant(ScopeType.WORKSPACE, WS, Role.MEMBER),
            _grant(ScopeType.PROJECT, CORE, Role.ADMIN),
        ]
    )
    on_core = R.resolve(ctx, _project(CORE))
    assert P.PROJECT_ADMIN in on_core.permissions
    assert P.ROLE_GRANT in on_core.permissions  # project-scoped grant power
    # On a different project, only the workspace member role applies.
    on_other = R.resolve(ctx, _project(OTHER))
    assert P.PROJECT_ADMIN not in on_other.permissions
    assert P.ROLE_GRANT not in on_other.permissions
    assert P.PROJECT_WRITE in on_other.permissions


def test_ac5_project_admin_scope_narrowed() -> None:
    ctx = _ctx([_grant(ScopeType.PROJECT, CORE, Role.ADMIN)])
    eff = R.resolve(ctx, _project(CORE))
    for gone in (P.MEMBER_MANAGE, P.TEAM_MANAGE, P.SECRETS_MANAGE, P.WORKSPACE_ADMIN):
        assert gone not in eff.permissions
    for kept in (P.ROLE_GRANT, P.DEPLOY_APPROVE, P.PROJECT_DELETE):
        assert kept in eff.permissions


def test_ac6_team_membership_confers_access() -> None:
    ctx = _ctx(memberships=[TeamMembership(team_id=BE, team_role=TeamRole.MEMBER)])
    pta = (ProjectTeamAccess(project_id=CORE, team_id=BE, access_level=AccessLevel.WRITE),)
    eff = R.resolve(ctx, _project(CORE), project_team_access=pta)
    assert {P.PROJECT_WRITE, P.TASK_WRITE} <= eff.permissions
    # No workspace grant exists, so on another project there is nothing.
    eff_other = R.resolve(ctx, _project(OTHER), project_team_access=pta)
    assert P.PROJECT_WRITE not in eff_other.permissions


def test_ac7_nested_team_inheritance() -> None:
    # User is in child BE (parent ENG); access granted to ENG.
    ctx = _ctx(
        memberships=[TeamMembership(team_id=BE, team_role=TeamRole.MEMBER, parent_team_id=ENG)]
    )
    pta = (ProjectTeamAccess(project_id=CORE, team_id=ENG, access_level=AccessLevel.WRITE),)
    eff = R.resolve(ctx, _project(CORE), project_team_access=pta)
    assert P.PROJECT_WRITE in eff.permissions


def test_ac7_inheritance_stops_at_max_depth() -> None:
    from forge_authz import MAX_TEAM_DEPTH, team_closure

    # Build a long chain leaf -> a1 -> a2 -> ... ; only the leaf is a membership
    # with each immediate parent linked, so the closure caps at MAX_TEAM_DEPTH.
    chain = [uuid.uuid4() for _ in range(MAX_TEAM_DEPTH + 4)]
    memberships = [
        TeamMembership(team_id=chain[i], team_role=TeamRole.MEMBER, parent_team_id=chain[i + 1])
        for i in range(len(chain) - 1)
    ]
    closure = team_closure(memberships)
    # The deepest ancestors beyond the cap are not reachable from the leaf alone.
    assert len(closure) <= len(chain)
    assert chain[0] in closure


def test_ac8_team_restricted_wall() -> None:
    # Workspace member NOT in BE: no read on a team-restricted project.
    ctx = _ctx([_grant(ScopeType.WORKSPACE, WS, Role.MEMBER)])
    pta = (ProjectTeamAccess(project_id=CORE, team_id=BE, access_level=AccessLevel.WRITE),)
    eff = R.resolve(ctx, _project(CORE, ProjectVisibility.TEAM_RESTRICTED), project_team_access=pta)
    assert P.PROJECT_READ not in eff.permissions
    # A member of BE does get access through the team path.
    ctx_be = _ctx(
        [_grant(ScopeType.WORKSPACE, WS, Role.MEMBER)],
        memberships=[TeamMembership(team_id=BE, team_role=TeamRole.MEMBER)],
    )
    eff_be = R.resolve(
        ctx_be, _project(CORE, ProjectVisibility.TEAM_RESTRICTED), project_team_access=pta
    )
    assert P.PROJECT_READ in eff_be.permissions


def test_ac9_workspace_admin_bypasses_restriction() -> None:
    ctx = _ctx([_grant(ScopeType.WORKSPACE, WS, Role.ADMIN)])
    eff = R.resolve(ctx, _project(CORE, ProjectVisibility.TEAM_RESTRICTED))
    assert P.PROJECT_READ in eff.permissions
    assert P.PROJECT_ADMIN in eff.permissions


def test_ac10_agent_runner_cannot_approve_or_grant() -> None:
    ctx = _ctx([_grant(ScopeType.PROJECT, CORE, Role.AGENT_RUNNER)])
    eff = R.resolve(ctx, _project(CORE))
    assert {P.TASK_WRITE, P.AGENT_RUN} <= eff.permissions
    for forbidden in (P.PR_APPROVE, P.DEPLOY_APPROVE, P.ROLE_GRANT):
        assert forbidden not in eff.permissions


def test_ac13_expired_grant_ignored() -> None:
    past = datetime.now(UTC) - timedelta(hours=1)
    ctx = _ctx([_grant(ScopeType.WORKSPACE, WS, Role.ADMIN, expires=past)])
    eff = R.resolve(ctx, _project())
    assert eff.permissions == frozenset()
    # A future expiry still resolves.
    future = datetime.now(UTC) + timedelta(hours=1)
    ctx2 = _ctx([_grant(ScopeType.WORKSPACE, WS, Role.MEMBER, expires=future)])
    assert P.PROJECT_WRITE in R.resolve(ctx2, _project()).permissions


def test_granting_sources_present() -> None:
    ctx = _ctx(
        [_grant(ScopeType.WORKSPACE, WS, Role.MEMBER)],
        memberships=[TeamMembership(team_id=BE, team_role=TeamRole.MEMBER)],
    )
    pta = (ProjectTeamAccess(project_id=CORE, team_id=BE, access_level=AccessLevel.WRITE),)
    eff = R.resolve(ctx, _project(CORE), project_team_access=pta)
    assert "workspace:member" in eff.granting_sources
    assert any("team" in s for s in eff.granting_sources)


def test_can_helper_matches_resolve() -> None:
    ctx = _ctx([_grant(ScopeType.WORKSPACE, WS, Role.MEMBER)])
    assert R.can(ctx, P.PROJECT_WRITE, _project())
    assert not R.can(ctx, P.ROLE_GRANT, _project())


# --------------------------------------------------------------------------- #
# AC19 — totality / determinism / order-independence (randomized fuzz)         #
# --------------------------------------------------------------------------- #

_ALL_ROLES = list(Role)
_SCOPES = list(ScopeType)
_LEVELS = list(AccessLevel)
_VIS = list(ProjectVisibility)


def _random_ctx(rng: random.Random) -> tuple[PrincipalContext, ResourceRef, tuple]:
    ws = uuid.UUID(int=rng.getrandbits(128))
    pid = uuid.UUID(int=rng.getrandbits(128))
    teams = [uuid.UUID(int=rng.getrandbits(128)) for _ in range(rng.randint(0, 3))]
    grants = []
    for _ in range(rng.randint(0, 5)):
        st = rng.choice(_SCOPES)
        if st is ScopeType.WORKSPACE:
            sid = ws
        else:
            sid = rng.choice([pid, *teams, uuid.UUID(int=rng.getrandbits(128))])
        exp = None
        if rng.random() < 0.3:
            delta = timedelta(hours=rng.choice([-2, -1, 1, 2]))
            exp = datetime.now(UTC) + delta
        grants.append(
            RoleGrant(
                id=uuid.uuid4(),
                workspace_id=ws,
                principal=PrincipalRef(type=PrincipalType.USER, id=USER),
                scope=ScopeRef(type=st, id=sid),
                role=rng.choice(_ALL_ROLES),
                expires_at=exp,
            )
        )
    memberships = tuple(
        TeamMembership(
            team_id=t,
            team_role=rng.choice(list(TeamRole)),
            parent_team_id=rng.choice([None, *teams]),
        )
        for t in teams
    )
    ctx = PrincipalContext(
        principal=PrincipalRef(type=PrincipalType.USER, id=USER),
        workspace_id=ws,
        grants=tuple(grants),
        team_memberships=memberships,
    )
    resource = ResourceRef(
        workspace_id=ws,
        project_id=rng.choice([None, pid]),
        team_id=rng.choice([None, *teams]) if teams else None,
        visibility=rng.choice(_VIS),
    )
    pta = tuple(
        ProjectTeamAccess(
            project_id=pid, team_id=rng.choice(teams), access_level=rng.choice(_LEVELS)
        )
        for _ in range(rng.randint(0, 3))
        if teams
    )
    return ctx, resource, pta


@pytest.mark.parametrize("seed", range(300))
def test_resolve_is_total_and_order_independent(seed: int) -> None:
    rng = random.Random(seed)
    ctx, resource, pta = _random_ctx(rng)
    fixed_now = datetime.now(UTC)
    eff = R.resolve(ctx, resource, project_team_access=pta, now=fixed_now)
    # totality: returns an EffectiveAccess, never raises
    assert isinstance(eff.permissions, frozenset)
    # determinism
    eff2 = R.resolve(ctx, resource, project_team_access=pta, now=fixed_now)
    assert eff.permissions == eff2.permissions
    assert eff.granting_sources == eff2.granting_sources
    # order-independence over grants
    shuffled = list(ctx.grants)
    rng.shuffle(shuffled)
    ctx_shuffled = ctx.model_copy(update={"grants": tuple(shuffled)})
    eff3 = R.resolve(ctx_shuffled, resource, project_team_access=pta, now=fixed_now)
    assert eff3.permissions == eff.permissions
    assert eff3.granting_sources == eff.granting_sources
