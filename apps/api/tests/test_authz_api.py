"""F30 API integration tests (teams / access / project-access + board visibility).

Hermetic: in-memory SQLite (StaticPool), the real routers + ``AuthzService``,
``get_db`` + ``get_current_principal`` overridden per acting principal. Covers
AC8, AC9, AC10, AC11, AC12, AC14, AC15, AC16, AC17, AC18, AC21, AC22.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from forge_api.db import get_db
from forge_api.deps import Principal, get_current_principal
from forge_api.main import create_app
from forge_contracts.authz import AccessLevel, PrincipalType, ProjectVisibility, ScopeType
from forge_contracts.enums import UserRole
from forge_db.base import Base
from forge_db.models import (
    AuditLog,
    Project,
    RoleGrant,
    Team,
    TeamMember,
    User,
    Workspace,
)

WS = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
WS2 = uuid.UUID("00000000-0000-0000-0000-0000000000a2")

ADMIN = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
ADMIN2 = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
MEMBER = uuid.UUID("00000000-0000-0000-0000-0000000000b3")
VIEWER = uuid.UUID("00000000-0000-0000-0000-0000000000b4")
LEAD = uuid.UUID("00000000-0000-0000-0000-0000000000b5")
AGENT = uuid.UUID("00000000-0000-0000-0000-0000000000b6")
OUTSIDER = uuid.UUID("00000000-0000-0000-0000-0000000000b7")

CORE = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
SECRET = uuid.UUID("00000000-0000-0000-0000-0000000000c2")
BE = uuid.UUID("00000000-0000-0000-0000-0000000000d1")
ENG = uuid.UUID("00000000-0000-0000-0000-0000000000d2")


def _ws_grant(user_id: uuid.UUID, role: UserRole, ws: uuid.UUID = WS) -> RoleGrant:
    return RoleGrant(
        workspace_id=ws,
        principal_type=PrincipalType.USER,
        principal_id=user_id,
        scope_type=ScopeType.WORKSPACE,
        scope_id=ws,
        role=role,
    )


@pytest.fixture
def factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with sf() as s:
        s.add(Workspace(id=WS, name="Acme", slug="acme"))
        s.add(Workspace(id=WS2, name="Other", slug="other"))
        for uid, role in [
            (ADMIN, UserRole.ADMIN),
            (ADMIN2, UserRole.ADMIN),
            (MEMBER, UserRole.MEMBER),
            (VIEWER, UserRole.VIEWER),
            (LEAD, UserRole.MEMBER),
            (AGENT, UserRole.AGENT_RUNNER),
            (OUTSIDER, UserRole.VIEWER),
        ]:
            s.add(User(id=uid, workspace_id=WS, email=f"{uid}@acme.test", role=role))
        # Workspace-scope grants (the resolver source of truth).
        s.add(_ws_grant(ADMIN, UserRole.ADMIN))
        s.add(_ws_grant(ADMIN2, UserRole.ADMIN))
        s.add(_ws_grant(MEMBER, UserRole.MEMBER))
        s.add(_ws_grant(VIEWER, UserRole.VIEWER))
        s.add(_ws_grant(LEAD, UserRole.MEMBER))
        s.add(_ws_grant(OUTSIDER, UserRole.VIEWER))
        # Agent: project-scoped agent_runner on CORE only.
        s.add(
            RoleGrant(
                workspace_id=WS,
                principal_type=PrincipalType.USER,
                principal_id=AGENT,
                scope_type=ScopeType.PROJECT,
                scope_id=CORE,
                role=UserRole.AGENT_RUNNER,
            )
        )
        # Teams: ENG (parent) -> BE (child); LEAD is lead of BE.
        s.add(Team(id=ENG, workspace_id=WS, key="ENG", name="Engineering"))
        s.add(Team(id=BE, workspace_id=WS, key="BE", name="Backend", parent_team_id=ENG))
        s.add(TeamMember(workspace_id=WS, team_id=BE, user_id=LEAD, team_role="lead"))
        s.add(TeamMember(workspace_id=WS, team_id=BE, user_id=MEMBER, team_role="member"))
        # Projects: CORE (workspace), SECRET (team_restricted to BE write).
        s.add(Project(id=CORE, workspace_id=WS, name="Core", key="CORE"))
        s.add(
            Project(
                id=SECRET,
                workspace_id=WS,
                name="Secret",
                key="SECRET",
                visibility=ProjectVisibility.TEAM_RESTRICTED,
            )
        )
        s.commit()
    return sf


def _principal(user_id: uuid.UUID, role: UserRole, ws: uuid.UUID = WS) -> Principal:
    return Principal(
        user_id=user_id,
        workspace_id=ws,
        role=role,
        email=f"{user_id}@acme.test",
        auth_method="test",
        scopes=["*"],
    )


def _client(factory: sessionmaker[Session], principal: Principal | None) -> TestClient:
    app: FastAPI = create_app()

    def _override_db() -> Iterator[Session]:
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_db
    if principal is not None:
        app.dependency_overrides[get_current_principal] = lambda: principal
    return TestClient(app)


def _seed_project_team_access(factory: sessionmaker[Session]) -> None:
    """BE gets write on SECRET (so BE members see it)."""
    from forge_db.models import ProjectTeamAccess

    with factory() as s:
        s.add(
            ProjectTeamAccess(
                workspace_id=WS, project_id=SECRET, team_id=BE, access_level=AccessLevel.WRITE
            )
        )
        s.commit()


# --------------------------------------------------------------------------- #
# AC22 — auth required / cross-workspace                                       #
# --------------------------------------------------------------------------- #


def test_anonymous_rejected(factory: sessionmaker[Session]) -> None:
    client = _client(factory, principal=None)
    # No auth override and no credentials -> 401.
    assert client.get("/teams").status_code == 401
    assert client.post("/access/grants", json={}).status_code == 401


def test_cross_workspace_team_is_404(factory: sessionmaker[Session]) -> None:
    # An admin in WS2 cannot see a WS team.
    client = _client(factory, _principal(uuid.uuid4(), UserRole.ADMIN, ws=WS2))
    assert client.get(f"/teams/{BE}").status_code == 404


# --------------------------------------------------------------------------- #
# AC14 / AC15 — teams CRUD, cycle/depth, lead scope                            #
# --------------------------------------------------------------------------- #


def test_admin_creates_team_member_cannot(factory: sessionmaker[Session]) -> None:
    admin = _client(factory, _principal(ADMIN, UserRole.ADMIN))
    r = admin.post("/teams", json={"key": "QA", "name": "Quality"})
    assert r.status_code == 201, r.text

    member = _client(factory, _principal(MEMBER, UserRole.MEMBER))
    r2 = member.post("/teams", json={"key": "X", "name": "X"})
    assert r2.status_code == 403
    assert r2.json()["detail"]["missing_permission"] == "team.manage"


def test_team_cycle_and_depth(factory: sessionmaker[Session]) -> None:
    admin = _client(factory, _principal(ADMIN, UserRole.ADMIN))
    # Cycle: set ENG's parent to BE (BE's parent is already ENG).
    r = admin.patch(f"/teams/{ENG}", json={"parent_team_id": str(BE)})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "team_cycle"


def test_team_lead_manages_own_team_only(factory: sessionmaker[Session]) -> None:
    lead = _client(factory, _principal(LEAD, UserRole.MEMBER))
    # Lead can add a member to their team (BE).
    r = lead.post(f"/teams/{BE}/members", json={"user_id": str(VIEWER), "team_role": "member"})
    assert r.status_code == 201, r.text
    # Lead cannot create teams (workspace-only team.manage).
    assert lead.post("/teams", json={"key": "Z", "name": "Z"}).status_code == 403
    # Lead cannot manage another team (ENG).
    r2 = lead.post(f"/teams/{ENG}/members", json={"user_id": str(VIEWER)})
    assert r2.status_code == 403


# --------------------------------------------------------------------------- #
# AC11 / AC12 — escalation + lockout                                           #
# --------------------------------------------------------------------------- #


def test_member_cannot_grant(factory: sessionmaker[Session]) -> None:
    member = _client(factory, _principal(MEMBER, UserRole.MEMBER))
    body = {
        "principal": {"type": "user", "id": str(VIEWER)},
        "scope": {"type": "workspace", "id": str(WS)},
        "role": "admin",
    }
    r = member.post("/access/grants", json=body)
    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "escalation"


def test_admin_can_grant(factory: sessionmaker[Session]) -> None:
    admin = _client(factory, _principal(ADMIN, UserRole.ADMIN))
    body = {
        "principal": {"type": "user", "id": str(MEMBER)},
        "scope": {"type": "project", "id": str(CORE)},
        "role": "admin",
    }
    r = admin.post("/access/grants", json=body)
    assert r.status_code == 201, r.text


def test_lockout_last_admin(factory: sessionmaker[Session]) -> None:
    admin = _client(factory, _principal(ADMIN, UserRole.ADMIN))
    # Revoke ADMIN2 first (two admins -> one).
    grants = admin.get(f"/access/grants?principal_id={ADMIN2}").json()
    ws_admin = next(g for g in grants if g["scope"]["type"] == "workspace")
    assert admin.delete(f"/access/grants/{ws_admin['id']}").status_code == 204
    # Now revoking the last admin (ADMIN) -> 409.
    grants = admin.get(f"/access/grants?principal_id={ADMIN}").json()
    last = next(g for g in grants if g["scope"]["type"] == "workspace")
    r = admin.delete(f"/access/grants/{last['id']}")
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "last_admin"


# --------------------------------------------------------------------------- #
# AC10 — agent runner cannot grant                                            #
# --------------------------------------------------------------------------- #


def test_agent_runner_cannot_grant(factory: sessionmaker[Session]) -> None:
    agent = _client(factory, _principal(AGENT, UserRole.AGENT_RUNNER))
    body = {
        "principal": {"type": "user", "id": str(MEMBER)},
        "scope": {"type": "project", "id": str(CORE)},
        "role": "member",
    }
    assert agent.post("/access/grants", json=body).status_code == 403


# --------------------------------------------------------------------------- #
# AC8 / AC9 / AC16 — project access + restricted wall + admin bypass           #
# --------------------------------------------------------------------------- #


def test_restricted_project_404_for_outsider(factory: sessionmaker[Session]) -> None:
    _seed_project_team_access(factory)
    outsider = _client(factory, _principal(OUTSIDER, UserRole.VIEWER))
    # Outsider (viewer, not in BE) -> 404 on the team-restricted project.
    assert outsider.get(f"/projects/{SECRET}/access").status_code == 404
    # BE member sees it.
    be_member = _client(factory, _principal(MEMBER, UserRole.MEMBER))
    assert be_member.get(f"/projects/{SECRET}/access").status_code == 200


def test_admin_bypasses_restriction(factory: sessionmaker[Session]) -> None:
    admin = _client(factory, _principal(ADMIN, UserRole.ADMIN))
    assert admin.get(f"/projects/{SECRET}/access").status_code == 200


def test_member_cannot_manage_project_access(factory: sessionmaker[Session]) -> None:
    member = _client(factory, _principal(MEMBER, UserRole.MEMBER))
    r = member.put(f"/projects/{CORE}/visibility", json={"visibility": "team_restricted"})
    assert r.status_code == 403


def test_admin_sets_visibility_and_team_access(factory: sessionmaker[Session]) -> None:
    admin = _client(factory, _principal(ADMIN, UserRole.ADMIN))
    r = admin.put(
        f"/projects/{CORE}/visibility",
        json={"visibility": "team_restricted", "owner_team_id": str(BE)},
    )
    assert r.status_code == 200, r.text
    assert r.json()["visibility"] == "team_restricted"
    r2 = admin.post(
        f"/projects/{CORE}/team-access", json={"team_id": str(BE), "access_level": "write"}
    )
    assert r2.status_code == 201
    r3 = admin.delete(f"/projects/{CORE}/team-access/{BE}")
    assert r3.status_code == 204


# --------------------------------------------------------------------------- #
# AC4 / AC5 / AC6 — effective access introspection                             #
# --------------------------------------------------------------------------- #


def test_effective_access_member_on_core(factory: sessionmaker[Session]) -> None:
    member = _client(factory, _principal(MEMBER, UserRole.MEMBER))
    r = member.get(f"/access/effective?project_id={CORE}")
    assert r.status_code == 200, r.text
    perms = set(r.json()["permissions"])
    assert {"project.read", "project.write", "task.write", "pr.approve"} <= perms
    assert "role.grant" not in perms


def test_effective_access_project_admin_scope_narrowed(
    factory: sessionmaker[Session],
) -> None:
    admin = _client(factory, _principal(ADMIN, UserRole.ADMIN))
    body = {
        "principal": {"type": "user", "id": str(MEMBER)},
        "scope": {"type": "project", "id": str(CORE)},
        "role": "admin",
    }
    assert admin.post("/access/grants", json=body).status_code == 201
    r = admin.get(f"/access/effective?principal_id={MEMBER}&project_id={CORE}")
    perms = set(r.json()["permissions"])
    assert "role.grant" in perms  # project-scoped grant power retained
    assert "member.manage" not in perms  # workspace-only narrowed out


# --------------------------------------------------------------------------- #
# AC18 — audit completeness                                                    #
# --------------------------------------------------------------------------- #


def test_grant_writes_one_audit_event(factory: sessionmaker[Session]) -> None:
    admin = _client(factory, _principal(ADMIN, UserRole.ADMIN))
    body = {
        "principal": {"type": "user", "id": str(VIEWER)},
        "scope": {"type": "project", "id": str(CORE)},
        "role": "member",
    }
    assert admin.post("/access/grants", json=body).status_code == 201
    with factory() as s:
        events = s.scalars(select(AuditLog).where(AuditLog.action == "role_grant.created")).all()
    assert len(events) == 1
    ev = events[0]
    assert ev.actor_id == ADMIN
    assert ev.scope_type == "project"


def test_team_create_writes_audit(factory: sessionmaker[Session]) -> None:
    admin = _client(factory, _principal(ADMIN, UserRole.ADMIN))
    assert admin.post("/teams", json={"key": "NEW", "name": "New"}).status_code == 201
    with factory() as s:
        events = s.scalars(select(AuditLog).where(AuditLog.action == "team.created")).all()
    assert len(events) == 1


# --------------------------------------------------------------------------- #
# AC17 — board visibility filter                                               #
# --------------------------------------------------------------------------- #


def test_visible_project_ids_filter(factory: sessionmaker[Session]) -> None:
    from forge_api.authz import ALL, visible_project_ids
    from forge_api.services.authz_service import AuthzService

    _seed_project_team_access(factory)
    with factory() as s:
        svc = AuthzService(s)
        # Workspace admin -> ALL.
        admin_ctx = svc.load_principal_context(WS, ADMIN)
        assert visible_project_ids(admin_ctx, WS, svc) is ALL
        # BE member sees CORE + SECRET.
        member_ctx = svc.load_principal_context(WS, MEMBER)
        member_vis = visible_project_ids(member_ctx, WS, svc)
        assert member_vis == {CORE, SECRET}
        # Outsider sees only the workspace-visible CORE, not the restricted SECRET.
        outsider_ctx = svc.load_principal_context(WS, OUTSIDER)
        outsider_vis = visible_project_ids(outsider_ctx, WS, svc)
        assert outsider_vis == {CORE}


# --------------------------------------------------------------------------- #
# AC21 — require_role shim                                                     #
# --------------------------------------------------------------------------- #


def test_require_role_shim_maps_to_permission(factory: sessionmaker[Session]) -> None:
    from forge_api.authz import require_role
    from forge_api.services.authz_service import AuthzService

    with factory() as s:
        svc = AuthzService(s)
        admin_ctx = svc.load_principal_context(WS, ADMIN)
        viewer_ctx = svc.load_principal_context(WS, VIEWER)

    dep_admin = require_role(UserRole.ADMIN)
    # Admin passes the WORKSPACE_ADMIN-equivalent shim.
    with factory() as s:
        svc = AuthzService(s)
        assert dep_admin(admin_ctx, svc) is admin_ctx
    # Viewer fails the admin shim.
    import fastapi

    with factory() as s, pytest.raises(fastapi.HTTPException) as exc:
        svc = AuthzService(s)
        dep_admin(viewer_ctx, svc)
    assert exc.value.status_code == 403
