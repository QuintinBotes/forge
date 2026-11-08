"""Spec L §6a: only a person decides an approval; personal tokens are how one does
it from another surface (seam 4, decisions 1-3 as revised by the lead, L1-L3).

Real authentication end to end: the process auth service verifies session JWTs
and API keys, the owners live in a database, and the approval authorizer is the
one that refuses.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, update

import forge_api.auth.service as auth_service_module
from forge_api.auth.service import AuthService
from forge_api.main import create_app
from forge_api.observability.redaction import redact_mapping
from forge_api.services.approval_service import build_gate_registry, get_approval_service
from forge_approval import ApprovalAuthorizer, ApprovalService, InMemoryApprovalRepository
from forge_approval.providers import InMemoryGrantStore
from forge_auth.tokens import encode_session_jwt
from forge_contracts.auth import SessionClaims
from forge_contracts.authz import PrincipalType, ScopeType
from forge_contracts.enums import UserRole
from forge_db.models import Project, RoleGrant, User, Workspace

AUTH_SECRET = "personal-token-test-secret"
WS = uuid.UUID("00000000-0000-0000-0000-0000000000e1")
MEMBER = uuid.UUID("00000000-0000-0000-0000-0000000000e2")
ADMIN = uuid.UUID("00000000-0000-0000-0000-0000000000e3")
PROJECT = uuid.UUID("00000000-0000-0000-0000-0000000000e4")


def session_jwt(user_id: uuid.UUID, role: UserRole, workspace_id: uuid.UUID = WS) -> str:
    now = int(time.time())
    claims = SessionClaims(
        sub=user_id, wsid=workspace_id, role=role, email="p@acme.test", exp=now + 3600, iat=now
    )
    return encode_session_jwt(claims, secret=AUTH_SECRET)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def auth(monkeypatch: pytest.MonkeyPatch) -> AuthService:
    """The process auth service (``get_current_principal`` calls it directly)."""
    service = AuthService(secret_key=b"7" * 32, auth_secret=AUTH_SECRET)
    monkeypatch.setattr(auth_service_module, "_default_service", service)
    return service


@pytest.fixture
def db(workflow_db: Any) -> Any:
    """The owners: an admin (with a workspace admin grant) and a member."""
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
    return workflow_db


@pytest.fixture
def client(auth: AuthService, db: Any) -> Iterator[TestClient]:
    approvals = ApprovalService(
        InMemoryApprovalRepository(),
        build_gate_registry(InMemoryGrantStore()),
        ApprovalAuthorizer(),
        redactor=redact_mapping,
    )
    app = db.install(create_app())
    app.dependency_overrides[get_approval_service] = lambda: approvals
    with TestClient(app) as c:
        yield c


def open_gate(client: TestClient, token: str, gate_type: str = "pr") -> str:
    resp = client.post(
        "/approvals",
        headers=bearer(token),
        json={
            "gate_type": gate_type,
            "subject_type": "changeset",
            "subject_id": str(uuid.uuid4()),
            "requested_actor": f"agent:{uuid.uuid4()}",
            "title": f"{gate_type} gate",
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def decide(client: TestClient, token: str, approval_id: str) -> Any:
    return client.post(
        f"/approvals/{approval_id}/decision", headers=bearer(token), json={"decision": "approve"}
    )


def mint_personal(
    client: TestClient, session: str, *, surface: str = "tervin", decide: bool = True, **extra: Any
) -> Any:
    return client.post(
        "/auth/personal-tokens",
        headers=bearer(session),
        json={"name": "laptop", "surface": surface, "decide": decide, **extra},
    )


# --------------------------------------------------------------------------- #
# L1: who decides                                                              #
# --------------------------------------------------------------------------- #


def test_an_unbound_admin_key_cannot_decide(client: TestClient, auth: AuthService) -> None:
    _, key = auth.bootstrap_key(workspace_id=WS, name="ci", role=UserRole.ADMIN)
    approval_id = open_gate(client, key)

    resp = decide(client, key, approval_id)

    assert resp.status_code == 403, resp.text
    assert "system principals can never resolve" in resp.json()["detail"]


def test_a_bound_unscoped_key_decides_as_its_owner(client: TestClient, auth: AuthService) -> None:
    # The web's pasted key, an SSO login key or the seed's bootstrap key.
    _, key = auth.bootstrap_key(workspace_id=WS, name="web", role=UserRole.ADMIN, user_id=ADMIN)
    approval_id = open_gate(client, session_jwt(MEMBER, UserRole.MEMBER), "policy_override")

    resp = decide(client, key, approval_id)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"


def test_an_agent_runner_token_is_an_agent(client: TestClient, auth: AuthService) -> None:
    _, key = auth.mint_agent_token(
        workspace_id=WS, name="run", role=UserRole.AGENT_RUNNER, user_id=MEMBER
    )
    approval_id = open_gate(client, session_jwt(ADMIN, UserRole.ADMIN))

    resp = decide(client, key, approval_id)

    assert resp.status_code == 403
    assert "agent principals can never resolve" in resp.json()["detail"]


def test_a_narrowed_read_key_cannot_decide(client: TestClient) -> None:
    session = session_jwt(MEMBER, UserRole.MEMBER)
    token = mint_personal(client, session, decide=False).json()["token"]
    approval_id = open_gate(client, session)

    resp = decide(client, token, approval_id)

    # Without approvals:decide the token is a machine principal, whatever its owner.
    assert resp.status_code == 403
    assert "system principals can never resolve" in resp.json()["detail"]


def test_session_decisions_are_unchanged(client: TestClient) -> None:
    approval_id = open_gate(client, session_jwt(ADMIN, UserRole.ADMIN))

    resp = decide(client, session_jwt(MEMBER, UserRole.MEMBER), approval_id)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"


def _personal_decide_token(client: TestClient, auth: AuthService) -> str:
    return str(mint_personal(client, session_jwt(ADMIN, UserRole.ADMIN)).json()["token"])


def _unscoped_admin_key(client: TestClient, auth: AuthService) -> str:
    return auth.bootstrap_key(workspace_id=WS, name="web", role=UserRole.ADMIN, user_id=ADMIN)[1]


def _demote_user_role(db: Any) -> None:  # an SSO login or SCIM remap rewrites User.role
    with db.factory() as s:
        s.execute(update(User).where(User.id == ADMIN).values(role=UserRole.MEMBER))
        s.commit()


def _demote_grant(db: Any) -> None:  # F30 lowers the workspace grant
    with db.factory() as s:
        s.execute(update(RoleGrant).where(RoleGrant.principal_id == ADMIN).values(role="member"))
        s.commit()


def _deactivate(db: Any) -> None:
    with db.factory() as s:
        s.execute(update(User).where(User.id == ADMIN).values(is_active=False))
        s.commit()


def _expire_grant(db: Any) -> None:  # a time-boxed workspace grant runs out
    with db.factory() as s:
        s.execute(
            update(RoleGrant)
            .where(RoleGrant.principal_id == ADMIN)
            .values(expires_at=datetime.now(UTC) - timedelta(days=1))
        )
        s.commit()


def _move_owner(db: Any) -> None:  # the owner now belongs to another workspace
    other = uuid.uuid4()
    with db.factory() as s:
        s.add(Workspace(id=other, name="Other", slug=f"other-{other.hex[:8]}"))
        s.flush()
        s.execute(update(User).where(User.id == ADMIN).values(workspace_id=other))
        s.commit()


def _delete_owner(db: Any) -> None:
    with db.factory() as s:
        s.execute(delete(RoleGrant).where(RoleGrant.principal_id == ADMIN))
        s.execute(delete(User).where(User.id == ADMIN))
        s.commit()


@pytest.mark.parametrize("credential", [_personal_decide_token, _unscoped_admin_key])
@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (_demote_user_role, "requires role 'admin'"),
        (_demote_grant, "requires role 'admin'"),
        (_deactivate, "system principals"),
        (_delete_owner, "system principals"),
        (_expire_grant, "system principals"),
        (_move_owner, "system principals"),
    ],
)
def test_a_key_decides_with_its_owners_current_role(
    client: TestClient, auth: AuthService, db: Any, credential: Any, change: Any, reason: str
) -> None:
    token = credential(client, auth)
    gate = open_gate(client, session_jwt(MEMBER, UserRole.MEMBER), "policy_override")
    spare = open_gate(client, session_jwt(MEMBER, UserRole.MEMBER), "policy_override")
    assert decide(client, token, spare).status_code == 200  # the admin owner decides

    change(db)

    resp = decide(client, token, gate)
    assert resp.status_code == 403, resp.text
    assert reason in resp.json()["detail"]


def test_a_keys_own_lower_role_bounds_an_admin_owner(client: TestClient, auth: AuthService) -> None:
    _, key = auth.bootstrap_key(workspace_id=WS, name="m", role=UserRole.MEMBER, user_id=ADMIN)
    approval_id = open_gate(client, session_jwt(MEMBER, UserRole.MEMBER), "policy_override")

    resp = decide(client, key, approval_id)

    assert resp.status_code == 403
    assert "actor is 'member'" in resp.json()["detail"]


def test_a_narrowed_key_is_a_viewer_everywhere_else(client: TestClient) -> None:
    token = mint_personal(client, session_jwt(ADMIN, UserRole.ADMIN)).json()["token"]

    assert client.get("/approvals", headers=bearer(token)).status_code == 200
    gate = {"gate_type": "pr", "subject_type": "changeset", "subject_id": str(uuid.uuid4())}
    assert client.post("/approvals", headers=bearer(token), json=gate).status_code == 403
    run = {"objective": "do something"}
    assert client.post("/agent/runs", headers=bearer(token), json=run).status_code == 403


# --------------------------------------------------------------------------- #
# L3: F30 routes see a narrowed key as a workspace viewer                      #
# --------------------------------------------------------------------------- #


def _f30_writes(client: TestClient, token: str) -> list[int]:
    grant = {
        "principal": {"type": "user", "id": str(MEMBER)},
        "scope": {"type": "workspace", "id": str(WS)},
        "role": "admin",
    }
    return [
        client.post("/teams", headers=bearer(token), json={"key": "T", "name": "t"}).status_code,
        client.post("/access/grants", headers=bearer(token), json=grant).status_code,
        client.put(
            f"/projects/{PROJECT}/visibility",
            headers=bearer(token),
            json={"visibility": "workspace"},
        ).status_code,
    ]


def test_a_read_token_from_an_admin_cannot_administer_f30(client: TestClient) -> None:
    for decide_scope in (False, True):
        session = session_jwt(ADMIN, UserRole.ADMIN)
        token = mint_personal(client, session, decide=decide_scope).json()["token"]
        assert _f30_writes(client, token) == [403, 403, 403]
    # The admin's own session does all three, so the grant behind the refusal is real.
    assert _f30_writes(client, session_jwt(ADMIN, UserRole.ADMIN)) == [201, 201, 200]


# --------------------------------------------------------------------------- #
# L2: POST /auth/personal-tokens                                               #
# --------------------------------------------------------------------------- #


class _Sink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit(self, event: Any) -> None:
        self.events.append(event)


def test_a_member_mints_a_personal_token_bound_to_themselves(
    client: TestClient, auth: AuthService, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = _Sink()
    monkeypatch.setattr(auth.audit, "sink", sink)

    resp = mint_personal(client, session_jwt(MEMBER, UserRole.MEMBER))

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["token"].startswith("forge_")
    # Stored as a viewer: code that ignores scopes fails closed.
    assert (body["user_id"], body["role"]) == (str(MEMBER), "viewer")
    assert (body["scopes"], body["surface"]) == (["approvals:decide", "read"], "tervin")
    expires = datetime.fromisoformat(body["expires_at"])
    assert abs(expires - (datetime.now(UTC) + timedelta(days=90))) < timedelta(minutes=1)
    (event,) = sink.events
    assert (event.action, event.actor_id, str(event.target_id)) == (
        "apikey.created",
        MEMBER,
        body["id"],
    )
    assert event.details["personal"] is True and event.details["surface"] == "tervin"
    assert "token" not in event.details

    me = client.get("/auth/me", headers=bearer(body["token"])).json()
    assert (me["role"], me["decides"], me["narrowed"], me["surface"]) == (
        "viewer",
        True,
        True,
        "tervin",
    )
    assert me["credential_id"] == body["id"]


def test_the_webs_unscoped_key_mints_a_personal_token(
    client: TestClient, auth: AuthService
) -> None:
    _, web_key = auth.bootstrap_key(
        workspace_id=WS, name="web", role=UserRole.MEMBER, user_id=MEMBER
    )

    resp = mint_personal(client, web_key)

    assert resp.status_code == 201, resp.text
    assert (resp.json()["user_id"], resp.json()["role"]) == (str(MEMBER), "viewer")


def test_narrowed_unbound_and_agent_keys_cannot_mint(client: TestClient, auth: AuthService) -> None:
    _, unbound = auth.bootstrap_key(workspace_id=WS, name="ci", role=UserRole.ADMIN)
    # An agent's token bound to the person who started its run must not mint them a decider.
    _, agent = auth.mint_agent_token(
        workspace_id=WS, name="run", role=UserRole.AGENT_RUNNER, user_id=MEMBER
    )
    session = session_jwt(MEMBER, UserRole.MEMBER)
    narrowed = [mint_personal(client, session, decide=d).json()["token"] for d in (True, False)]

    for token in (unbound, agent, *narrowed):
        resp = mint_personal(client, token)
        assert resp.status_code == 403, resp.text
        assert "minted by a person" in resp.json()["detail"]


@pytest.mark.parametrize("surface", ["web", "system", "api", "slack"])
def test_minting_refuses_surfaces_a_personal_token_cannot_name(
    client: TestClient, surface: str
) -> None:
    resp = mint_personal(client, session_jwt(MEMBER, UserRole.MEMBER), surface=surface)
    assert resp.status_code == 422


@pytest.mark.parametrize("days", [0, 366])
def test_minting_bounds_the_expiry(client: TestClient, days: int) -> None:
    resp = mint_personal(client, session_jwt(MEMBER, UserRole.MEMBER), expires_in_days=days)
    assert resp.status_code == 422


@pytest.mark.parametrize(
    ("scopes", "surface"),
    [(["write"], None), (["approvals:decide"], None), (["read", "admin"], None), (None, "system")],
)
def test_the_key_store_refuses_other_scopes_and_surfaces(
    auth: AuthService, scopes: list[str] | None, surface: str | None
) -> None:
    with pytest.raises(ValueError, match="unsupported key"):
        auth.api_keys.mint(
            workspace_id=WS,
            name="x",
            role=UserRole.MEMBER,
            user_id=MEMBER,
            scopes=scopes,
            surface=surface,
        )


def test_an_unbound_key_can_never_carry_approvals_decide(auth: AuthService) -> None:
    with pytest.raises(ValueError, match="approvals:decide needs a person"):
        auth.api_keys.mint(
            workspace_id=WS, name="ci", role=UserRole.ADMIN, scopes=["read", "approvals:decide"]
        )


def test_the_key_store_stores_a_narrowed_key_as_a_viewer(auth: AuthService) -> None:
    info, token = auth.api_keys.mint(
        workspace_id=WS, name="x", role=UserRole.ADMIN, user_id=ADMIN, scopes=["read"]
    )
    assert info.role is UserRole.VIEWER
    record = auth.api_keys.verify(token)
    assert record is not None and record.role is UserRole.VIEWER


def test_a_lower_role_key_cannot_mint_a_token_that_decides_above_it(
    client: TestClient, auth: AuthService
) -> None:
    _, key = auth.bootstrap_key(workspace_id=WS, name="m", role=UserRole.MEMBER, user_id=ADMIN)

    resp = mint_personal(client, key)

    assert resp.status_code == 403, resp.text
    assert mint_personal(client, key, decide=False).status_code == 201  # a read token is fine


def test_a_deactivated_owner_cannot_mint(client: TestClient, db: Any) -> None:
    session = session_jwt(ADMIN, UserRole.ADMIN)  # still cryptographically valid
    _deactivate(db)

    assert mint_personal(client, session).status_code == 403
    assert mint_personal(client, session, decide=False).status_code == 403


def test_a_deciding_token_sees_its_inbox(client: TestClient) -> None:
    token = mint_personal(client, session_jwt(ADMIN, UserRole.ADMIN)).json()["token"]
    gate = open_gate(client, session_jwt(MEMBER, UserRole.MEMBER), "pr")

    mine = client.get(
        "/approvals", params={"status": "pending", "mine": True}, headers=bearer(token)
    )
    count = client.get("/approvals/count", params={"mine": True}, headers=bearer(token))

    assert gate in [a["id"] for a in mine.json()]
    assert count.json()["count"] >= 1
