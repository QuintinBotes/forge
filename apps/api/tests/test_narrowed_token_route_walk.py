"""Walk the whole route table with narrowed personal tokens and an agent's token.

An admin mints a read token and a run token (``read`` + ``runs:execute``; spec L §6a, §6b;
#206), and an agent's token is minted for a run the admin started (``mint_agent_token``;
#188). Every route guarded by ``require_permission`` that needs a permission the token's
role lacks must answer 403. Every route that authorises any other way is listed in
``UNGUARDED`` (``tests/security/route_matrix.py``) with the reason a token gains nothing
there; the writes among them that take an API key are called in :func:`_refused` and must
answer 403 too. The F30 administrative calls among them answer 403 to every role below
admin as well.
"""

from __future__ import annotations

import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import scf_api.auth.service as auth_service_module
from scf_api.auth.rbac import permissions_for
from scf_api.auth.service import AuthService
from scf_api.main import create_app
from scf_api.settings import Settings
from scf_auth.tokens import encode_session_jwt
from scf_contracts.auth import SessionClaims
from scf_contracts.authz import PrincipalType, ScopeType
from scf_contracts.enums import UserRole
from scf_db.models import Project, RoleGrant, User, Workspace

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests" / "security"))
from route_matrix import UNGUARDED, routes, url

AUTH_SECRET = "route-walk-test-secret"
WS = uuid.UUID("00000000-0000-0000-0000-0000000000f1")
ADMIN = uuid.UUID("00000000-0000-0000-0000-0000000000f2")
MEMBER = uuid.UUID("00000000-0000-0000-0000-0000000000f3")
PROJECT = uuid.UUID("00000000-0000-0000-0000-0000000000f4")

#: What each token's role holds (a read token is a viewer, a run or agent token an
#: agent-runner).
HELD = {
    "read": permissions_for(UserRole.VIEWER),
    "run": permissions_for(UserRole.AGENT_RUNNER),
    "agent": permissions_for(UserRole.AGENT_RUNNER),
}


def session_jwt(user_id: uuid.UUID, role: UserRole) -> str:
    now = int(time.time())
    claims = SessionClaims(
        sub=user_id, wsid=WS, role=role, email="p@acme.test", exp=now + 3600, iat=now
    )
    return encode_session_jwt(claims, secret=AUTH_SECRET)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, workflow_db: Any) -> Iterator[TestClient]:
    service = AuthService(secret_key=b"8" * 32, auth_secret=AUTH_SECRET)
    monkeypatch.setattr(auth_service_module, "_default_service", service)
    with workflow_db.factory() as s:
        s.add(Workspace(id=WS, name="Acme", slug="acme"))
        s.flush()
        s.add(User(id=ADMIN, workspace_id=WS, email="a@acme.test", role=UserRole.ADMIN))
        s.add(User(id=MEMBER, workspace_id=WS, email="m@acme.test", role=UserRole.MEMBER))
        s.add(
            RoleGrant(
                workspace_id=WS,
                principal_type=PrincipalType.USER,
                principal_id=ADMIN,
                scope_type=ScopeType.WORKSPACE,
                scope_id=WS,
                role=UserRole.ADMIN,
            )
        )
        s.add(Project(id=PROJECT, workspace_id=WS, name="Core", key="CORE"))
        s.commit()
    # Hundreds of calls with one token would trip the per-caller rate limit; every
    # settings-gated route is mounted so the walk sees it.
    settings = Settings(ratelimit_enabled=False, public_leaderboard_enabled=True)
    app = workflow_db.install(create_app(settings))
    with TestClient(app) as c:
        yield c


def _mint(client: TestClient, admin: str, kind: str) -> str:
    if kind == "agent":  # bound to the admin who started its run
        service = auth_service_module.get_auth_service()
        return service.mint_agent_token(workspace_id=WS, name="run", user_id=ADMIN)[1]
    resp = client.post(
        "/auth/personal-tokens",
        headers=bearer(admin),
        json={"name": kind, "surface": "tervin", "start_runs": kind == "run"},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["token"])


def _refused(client: TestClient, admin: str, kind: str) -> list[tuple[str, str, Any]]:
    """The listed writes that take an API key: each must refuse the token."""
    calls = [
        ("POST", "/auth/personal-tokens", {"name": "t", "surface": "cli"}),
        *_administrative(client, admin),
        ("POST", "/world/decisions", {"title": "ship it", "fact_ids": []}),
    ]
    if kind == "read":  # the run token holds run_agent, which proposing admits
        calls.append(("POST", "/world/proposals", {}))
    return calls


def _administrative(client: TestClient, admin: str) -> list[tuple[str, str, Any]]:
    """The F30 calls only a workspace admin makes here (no caller leads the team)."""
    team = client.post("/teams", headers=bearer(admin), json={"key": "CORE", "name": "Core"})
    team_id = team.json()["id"]
    member = {"user_id": str(MEMBER), "team_role": "member"}
    assert client.post(f"/teams/{team_id}/members", headers=bearer(admin), json=member).is_success
    grant_body = {
        "principal": {"type": "user", "id": str(MEMBER)},
        "scope": {"type": "workspace", "id": str(WS)},
        "role": "viewer",
    }
    grant = client.post("/access/grants", headers=bearer(admin), json=grant_body).json()["id"]
    access = {"team_id": team_id, "access_level": "write"}
    return [
        ("POST", "/teams", {"key": "OPS", "name": "Ops"}),
        ("PATCH", f"/teams/{team_id}", {"name": "renamed"}),
        ("POST", f"/teams/{team_id}/archive", None),
        ("POST", f"/teams/{team_id}/members", {"user_id": str(ADMIN)}),
        ("PATCH", f"/teams/{team_id}/members/{MEMBER}", {"team_role": "lead"}),
        ("DELETE", f"/teams/{team_id}/members/{MEMBER}", None),
        ("GET", "/access/grants", None),
        ("POST", "/access/grants", {**grant_body, "role": "admin"}),
        ("DELETE", f"/access/grants/{grant}", None),
        ("PUT", f"/projects/{PROJECT}/visibility", {"visibility": "workspace"}),
        ("POST", f"/projects/{PROJECT}/team-access", access),
        ("DELETE", f"/projects/{PROJECT}/team-access/{team_id}", None),
    ]


@pytest.mark.parametrize("kind", ["read", "run", "agent"])
def test_every_route_refuses_what_a_token_cannot_do(client: TestClient, kind: str) -> None:
    admin = session_jwt(ADMIN, UserRole.ADMIN)
    token = _mint(client, admin, kind)

    unguarded: set[str] = set()
    not_refused: list[str] = []
    called = 0
    for method, path, needs in routes(client.app):
        if needs is None:
            unguarded.add(f"{method} {path}")
        elif not needs <= HELD[kind]:
            called += 1
            resp = client.request(method, url(path), headers=bearer(token))
            if resp.status_code != 403:
                not_refused.append(f"{method} {path} -> {resp.status_code}")

    assert not_refused == []
    # A route added without require_permission must be guarded or listed with its reason.
    assert sorted(unguarded - UNGUARDED.keys()) == []
    assert sorted(UNGUARDED.keys() - unguarded) == []  # no stale entries
    assert called > 100  # the walk saw the guarded routes

    calls = _refused(client, admin, kind)
    for method, path, body in calls:
        resp = client.request(method, path, headers=bearer(token), json=body)
        assert resp.status_code == 403, f"{method} {path} -> {resp.status_code}: {resp.text}"
    # The token's owner is not refused, so each 403 above is the token's own limit.
    for method, path, body in calls:
        resp = client.request(method, path, headers=bearer(admin), json=body)
        assert resp.status_code != 403, f"{method} {path} -> {resp.status_code}: {resp.text}"
    # The listed reason for SCIM holds: an API key is not a SCIM token.
    assert client.get("/scim/v2/Users", headers=bearer(token)).status_code == 401


@pytest.mark.parametrize("role", [UserRole.MEMBER, UserRole.VIEWER, UserRole.AGENT_RUNNER])
def test_every_role_below_admin_is_refused_the_f30_administrative_calls(
    client: TestClient, workflow_db: Any, role: UserRole
) -> None:
    person = uuid.uuid4()
    with workflow_db.factory() as s:
        s.add(User(id=person, workspace_id=WS, email="r@acme.test", role=role))
        s.add(
            RoleGrant(
                workspace_id=WS,
                principal_type=PrincipalType.USER,
                principal_id=person,
                scope_type=ScopeType.WORKSPACE,
                scope_id=WS,
                role=role,
            )
        )
        s.commit()

    # test_every_route_refuses_what_a_token_cannot_do shows the admin is not refused these.
    for method, path, body in _administrative(client, session_jwt(ADMIN, UserRole.ADMIN)):
        resp = client.request(method, path, headers=bearer(session_jwt(person, role)), json=body)
        assert resp.status_code == 403, f"{method} {path} -> {resp.status_code}: {resp.text}"
