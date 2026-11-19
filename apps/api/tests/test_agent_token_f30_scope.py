"""F30 access of an agent's token and of a person holding the agent-runner role (#188).

An agent's token (``mint_agent_token``) keeps the scopes of the person it is bound to,
capped at what an agent-runner holds, so it manages no team and no project. A person
signed in with the agent-runner role keeps their own grants and team memberships.
``GET /access/effective`` reports what the routes enforce for the same caller.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

import scf_api.auth.service as auth_service_module
from scf_api.auth.service import AuthService
from scf_api.main import create_app
from scf_api.settings import Settings
from scf_auth.tokens import encode_session_jwt
from scf_authz.permissions import ROLE_PERMISSIONS
from scf_contracts.auth import SessionClaims
from scf_contracts.authz import AccessLevel, PrincipalType, ProjectVisibility, Role, ScopeType
from scf_contracts.enums import UserRole
from scf_db.models import (
    Project,
    ProjectTeamAccess,
    RoleGrant,
    Team,
    TeamMember,
    User,
    Workspace,
)

AUTH_SECRET = "agent-scope-test-secret"
WS = uuid.UUID("00000000-0000-0000-0000-0000000000e1")
PERSON = uuid.UUID("00000000-0000-0000-0000-0000000000e2")  # agent-runner role, leads TEAM
MATE = uuid.UUID("00000000-0000-0000-0000-0000000000e3")
CORE = uuid.UUID("00000000-0000-0000-0000-0000000000e4")  # PERSON's project-scope grant
OTHER = uuid.UUID("00000000-0000-0000-0000-0000000000e5")  # workspace-visible, no grant
WALLED = uuid.UUID("00000000-0000-0000-0000-0000000000e6")  # team-restricted, TEAM admin
TEAM = uuid.UUID("00000000-0000-0000-0000-0000000000e7")

AGENT_RUNNER = {p.value for p in ROLE_PERMISSIONS[Role.AGENT_RUNNER]}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, workflow_db: Any) -> Iterator[TestClient]:
    monkeypatch.setattr(
        auth_service_module,
        "_default_service",
        AuthService(secret_key=b"9" * 32, auth_secret=AUTH_SECRET),
    )
    with workflow_db.factory() as s:
        s.add(Workspace(id=WS, name="Acme", slug="acme"))
        s.flush()
        s.add(User(id=PERSON, workspace_id=WS, email="p@acme.test", role=UserRole.AGENT_RUNNER))
        s.add(User(id=MATE, workspace_id=WS, email="m@acme.test", role=UserRole.MEMBER))
        s.add(
            RoleGrant(
                workspace_id=WS,
                principal_type=PrincipalType.USER,
                principal_id=PERSON,
                scope_type=ScopeType.PROJECT,
                scope_id=CORE,
                role=UserRole.AGENT_RUNNER,
            )
        )
        s.add(Project(id=CORE, workspace_id=WS, name="Core", key="CORE"))
        s.add(Project(id=OTHER, workspace_id=WS, name="Other", key="OTHER"))
        walled = ProjectVisibility.TEAM_RESTRICTED
        s.add(Project(id=WALLED, workspace_id=WS, name="Walled", key="WALL", visibility=walled))
        s.add(Team(id=TEAM, workspace_id=WS, key="T", name="Team"))
        s.flush()
        s.add(TeamMember(workspace_id=WS, team_id=TEAM, user_id=PERSON, team_role="lead"))
        s.add(TeamMember(workspace_id=WS, team_id=TEAM, user_id=MATE, team_role="member"))
        admin = AccessLevel.ADMIN
        s.add(
            ProjectTeamAccess(workspace_id=WS, project_id=WALLED, team_id=TEAM, access_level=admin)
        )
        s.commit()
    app = workflow_db.install(create_app(Settings(ratelimit_enabled=False)))
    with TestClient(app) as c:
        yield c


def _session() -> str:
    now = int(time.time())
    claims = SessionClaims(
        sub=PERSON, wsid=WS, role=UserRole.AGENT_RUNNER, email="p@acme.test", exp=now + 60, iat=now
    )
    return encode_session_jwt(claims, secret=AUTH_SECRET)


def _agent_token() -> str:
    service = auth_service_module.get_auth_service()
    return service.mint_agent_token(workspace_id=WS, name="run", user_id=PERSON)[1]


def _projects(client: TestClient, token: str) -> set[str]:
    return {p["key"] for p in client.get("/projects", headers=bearer(token)).json()}


def _effective(client: TestClient, token: str, project: uuid.UUID) -> set[str]:
    resp = client.get("/access/effective", headers=bearer(token), params={"project_id": project})
    assert resp.status_code == 200, resp.text
    return set(resp.json()["permissions"])


def test_a_person_with_the_agent_runner_role_keeps_their_grants_and_team(
    client: TestClient,
) -> None:
    person = _session()

    assert _projects(client, person) == {"CORE", "WALL"}  # not OTHER: no grant there
    assert client.get(f"/projects/{OTHER}/access", headers=bearer(person)).status_code == 404
    assert client.get(f"/projects/{WALLED}/access", headers=bearer(person)).status_code == 200
    lead = {"team_role": "member"}
    resp = client.patch(f"/teams/{TEAM}/members/{MATE}", headers=bearer(person), json=lead)
    assert resp.status_code == 200, resp.text
    assert {"project.admin", "team.member.manage"} <= _effective(client, person, WALLED)


def test_an_agent_token_keeps_its_owners_scopes_capped_at_agent_runner(
    client: TestClient,
) -> None:
    agent = _agent_token()

    assert _projects(client, agent) == {"CORE", "WALL"}
    assert client.get(f"/projects/{OTHER}/access", headers=bearer(agent)).status_code == 404
    lead = {"team_role": "member"}
    resp = client.patch(f"/teams/{TEAM}/members/{MATE}", headers=bearer(agent), json=lead)
    assert resp.status_code == 403, resp.text
    visibility = {"visibility": "workspace"}
    resp = client.put(f"/projects/{WALLED}/visibility", headers=bearer(agent), json=visibility)
    assert resp.status_code == 403, resp.text
    # What /access/effective reports is what the routes above enforced.
    assert _effective(client, agent, WALLED) <= AGENT_RUNNER
    assert "project.read" in _effective(client, agent, WALLED)
