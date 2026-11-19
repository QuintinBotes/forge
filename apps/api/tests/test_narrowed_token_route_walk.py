"""Walk the whole route table with narrowed personal tokens (spec L §6a, §6b; #206).

An admin mints a read token and a run token (``read`` + ``runs:execute``). Every route
guarded by ``require_permission`` that needs a permission the token's role lacks must
answer 403. Every route that authorises any other way is listed in :data:`UNGUARDED`
with the reason a narrowed token gains nothing there; the writes among them that take
an API key are called in :func:`_refused` and must answer 403 too.
"""

from __future__ import annotations

import inspect
import re
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, APIWebSocketRoute, iter_route_contexts
from fastapi.testclient import TestClient

import scf_api.auth.service as auth_service_module
from scf_api.auth.rbac import Permission, permissions_for
from scf_api.auth.service import AuthService
from scf_api.main import create_app
from scf_api.settings import Settings
from scf_auth.tokens import encode_session_jwt
from scf_contracts.auth import SessionClaims
from scf_contracts.authz import PrincipalType, ScopeType
from scf_contracts.enums import UserRole
from scf_db.models import Project, RoleGrant, User, Workspace

AUTH_SECRET = "route-walk-test-secret"
WS = uuid.UUID("00000000-0000-0000-0000-0000000000f1")
ADMIN = uuid.UUID("00000000-0000-0000-0000-0000000000f2")
MEMBER = uuid.UUID("00000000-0000-0000-0000-0000000000f3")
PROJECT = uuid.UUID("00000000-0000-0000-0000-0000000000f4")

#: What each token's role holds (a read token is a viewer, a run token an agent-runner).
HELD = {
    "read": permissions_for(UserRole.VIEWER),
    "run": permissions_for(UserRole.AGENT_RUNNER),
}

_F30 = (
    "F30: authorised in AuthzService against get_principal_context, which gives a narrowed "
    "key one workspace viewer grant and none of its owner's; the writes are called below"
)
_SIGN_IN = "Sign-in: reads no API key, only what an identity provider or a one-time link returns"
_WEBHOOK = "Webhook: verified by the sender's signature or shared secret, never an API key"
_SCIM = "SCIM: require_scim_token accepts only a SCIM bearer token; an API key gets 401"
_READ = "Reads the caller's own workspace and changes nothing; every role holds read"
_PUBLIC = "Public by design: liveness, service info and API docs read no credential"

#: Every route without a ``require_permission`` guard, and why a narrowed token gains
#: nothing there. Each handler was read before it was listed.
UNGUARDED: dict[str, str] = {
    **dict.fromkeys(
        [
            "GET /health",
            "GET /healthz",
            "GET /readyz",
            "GET /health/ready",
            "GET /",
            "GET /openapi.json",
            "GET /docs",
            "GET /docs/oauth2-redirect",
            "GET /redoc",
        ],
        _PUBLIC,
    ),
    **dict.fromkeys(
        [
            "POST /auth/login",
            "POST /auth/callback",
            "GET /auth/sign-in-link",
            "GET /auth/saml/discover",
            "POST /auth/saml/discover",
            "GET /auth/saml/{slug}/metadata",
            "GET /auth/saml/{slug}/login",
            "POST /auth/saml/{slug}/acs",
            "GET /auth/saml/{slug}/slo",
            "POST /auth/saml/{slug}/slo",
            "GET /auth/oidc/{slug}/login",
            "GET /auth/oidc/{slug}/callback",
        ],
        _SIGN_IN,
    ),
    "POST /auth/logout": "Revokes only the key in the session cookie; reads no header token",
    "POST /auth/personal-tokens": "Refuses every narrowed key, so no token mints a token "
    "(called below)",
    **dict.fromkeys(
        [
            "POST /integration/github/webhooks",
            "POST /integration/slack/commands",
            "POST /integration/slack/interactions",
            "POST /integrations/alerts/{provider}/webhook",
            "POST /integrations/pm/webhooks/jira/{connection_id}",
            "POST /integrations/pm/webhooks/linear/{connection_id}",
            "POST /integrations/pm/webhooks/github_issues/{connection_id}",
        ],
        _WEBHOOK,
    ),
    **dict.fromkeys(
        [
            "GET /scim/v2/ServiceProviderConfig",
            "GET /scim/v2/ResourceTypes",
            "GET /scim/v2/Schemas",
            "GET /scim/v2/Users",
            "POST /scim/v2/Users",
            "GET /scim/v2/Users/{scim_id}",
            "PUT /scim/v2/Users/{scim_id}",
            "PATCH /scim/v2/Users/{scim_id}",
            "DELETE /scim/v2/Users/{scim_id}",
            "GET /scim/v2/Groups",
            "POST /scim/v2/Groups",
            "GET /scim/v2/Groups/{scim_id}",
            "PUT /scim/v2/Groups/{scim_id}",
            "PATCH /scim/v2/Groups/{scim_id}",
            "DELETE /scim/v2/Groups/{scim_id}",
        ],
        _SCIM,
    ),
    **dict.fromkeys(
        [
            "GET /auth/me",
            "GET /board/tasks",
            "GET /board/tasks/{task_id}",
            "GET /board/epics",
            "GET /board/epics/{epic_id}",
            "GET /board/sprints",
            "GET /board/sprints/{sprint_id}",
            "GET /board/milestones",
            "GET /board/milestones/{milestone_id}",
            "GET /board/incidents",
            "GET /board/incidents/{incident_id}",
            "POST /knowledge/search",
            "GET /observability/audit",
            "GET /observability/metrics",
            "GET /observability/runs/{run_id}/trace",
        ],
        _READ,
    ),
    "WS /ws": "Pushes the workspace's events to the caller; what the client sends is ignored",
    "WS /ws/spec/{spec_id}": "Closes a socket that sends an edit without write "
    "(can(principal.role, WRITE)); neither token holds write",
    "POST /world/proposals": "Needs write or run_agent (_require_write_or_run_agent): the "
    "read token is refused (called below); create_proposal records the run token's "
    "proposal as an agent's (SourceKind.AGENT)",
    "POST /world/decisions": "Needs write or run_agent, and refuses every narrowed key "
    "(_require_decision_recorder), whose decision would be stored as its owner's own: both "
    "tokens are refused (called below)",
    **dict.fromkeys(
        ["GET /mcp", "POST /mcp"],
        "Hides and refuses write tools without write or run_agent (registry.can_write); "
        "test_a_narrowed_token_on_mcp_is_narrowed_as_on_ws",
    ),
    **dict.fromkeys(
        [
            "GET /teams",
            "POST /teams",
            "GET /teams/{team_id}",
            "PATCH /teams/{team_id}",
            "POST /teams/{team_id}/archive",
            "GET /teams/{team_id}/members",
            "POST /teams/{team_id}/members",
            "PATCH /teams/{team_id}/members/{user_id}",
            "DELETE /teams/{team_id}/members/{user_id}",
            "GET /access/grants",
            "POST /access/grants",
            "DELETE /access/grants/{grant_id}",
            "GET /access/effective",
            "GET /projects",
            "GET /projects/{project_id}/access",
            "PUT /projects/{project_id}/visibility",
            "GET /projects/{project_id}/team-access",
            "POST /projects/{project_id}/team-access",
            "DELETE /projects/{project_id}/team-access/{team_id}",
        ],
        _F30,
    ),
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
    # Hundreds of calls with one token would trip the per-caller rate limit.
    app = workflow_db.install(create_app(Settings(ratelimit_enabled=False)))
    with TestClient(app) as c:
        yield c


def _guard(dependant: Dependant) -> set[Permission] | None:
    """The permissions the route's ``require_permission`` guards need; ``None`` if it has none."""
    needs: set[Permission] = set()
    found = False
    stack = list(dependant.dependencies)
    while stack:
        dep = stack.pop()
        stack.extend(dep.dependencies)
        call = dep.call
        if getattr(call, "__qualname__", "") == "require_permission.<locals>._dependency":
            permission = inspect.getclosurevars(call).nonlocals["permission"]
            if isinstance(permission, Permission):  # not F30's scoped permission
                needs.add(permission)
                found = True
    return needs if found else None


def _routes(app: Any) -> Iterator[tuple[str, str, set[Permission] | None]]:
    for route in iter_route_contexts(app.routes):
        original = route.original_route
        if isinstance(original, APIWebSocketRoute):
            yield "WS", route.path, None
            continue
        needs = _guard(route.dependant) if isinstance(original, APIRoute) else None
        for method in sorted((route.methods or set()) - {"HEAD"}):
            yield method, route.path, needs


def _url(path: str) -> str:
    return re.sub(r"{[^}]+}", lambda _: str(uuid.uuid4()), path)


def _mint(client: TestClient, admin: str, kind: str) -> str:
    resp = client.post(
        "/auth/personal-tokens",
        headers=bearer(admin),
        json={"name": kind, "surface": "tervin", "start_runs": kind == "run"},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["token"])


def _refused(client: TestClient, admin: str, kind: str) -> list[tuple[str, str, Any]]:
    """The listed writes that take an API key: each must refuse a narrowed token."""
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
    calls: list[tuple[str, str, Any]] = [
        ("POST", "/auth/personal-tokens", {"name": "t", "surface": "cli"}),
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
        ("POST", "/world/decisions", {"title": "ship it", "fact_ids": []}),
    ]
    if kind == "read":  # the run token holds run_agent, which proposing admits
        calls.append(("POST", "/world/proposals", {}))
    return calls


@pytest.mark.parametrize("kind", ["read", "run"])
def test_every_route_refuses_what_a_narrowed_token_cannot_do(client: TestClient, kind: str) -> None:
    admin = session_jwt(ADMIN, UserRole.ADMIN)
    token = _mint(client, admin, kind)

    unguarded: set[str] = set()
    not_refused: list[str] = []
    called = 0
    for method, path, needs in _routes(client.app):
        if needs is None:
            unguarded.add(f"{method} {path}")
        elif not needs <= HELD[kind]:
            called += 1
            resp = client.request(method, _url(path), headers=bearer(token))
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
    # The token's owner is not refused, so each 403 above is the token's narrowing.
    for method, path, body in calls:
        resp = client.request(method, path, headers=bearer(admin), json=body)
        assert resp.status_code != 403, f"{method} {path} -> {resp.status_code}: {resp.text}"
    # The listed reason for SCIM holds: an API key is not a SCIM token.
    assert client.get("/scim/v2/Users", headers=bearer(token)).status_code == 401
