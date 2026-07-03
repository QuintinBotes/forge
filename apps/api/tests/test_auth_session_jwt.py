"""F37 session-JWT resolution through the API auth layer (AC11).

A valid HS256 JWT (correct ``aud``, unexpired, signed with the shared
``AUTH_SECRET``) resolves a ``user`` principal with the claim's role/workspace;
expired / wrong-secret / wrong-audience JWTs are rejected with 401. API-key
auth keeps working alongside.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator

import httpx
import pytest

from forge_api.auth.service import AuthService, get_auth_service
from forge_api.main import app
from forge_auth.tokens import encode_session_jwt
from forge_contracts.auth import SessionClaims
from forge_contracts.enums import UserRole

AUTH_SECRET = "test-auth-secret"
WS = uuid.uuid4()
USER_ID = uuid.uuid4()


@pytest.fixture
def service() -> Iterator[AuthService]:
    svc = AuthService(secret_key=b"2" * 32, auth_secret=AUTH_SECRET)
    app.dependency_overrides[get_auth_service] = lambda: svc
    try:
        yield svc
    finally:
        app.dependency_overrides.pop(get_auth_service, None)


@pytest.fixture
async def client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


def _jwt(
    *,
    role: UserRole = UserRole.MEMBER,
    exp_offset: int = 3600,
    aud: str = "forge-api",
    secret: str = AUTH_SECRET,
) -> str:
    now = int(time.time())
    claims = SessionClaims(
        sub=USER_ID,
        wsid=WS,
        role=role,
        email="alice@example.com",
        aud=aud,
        exp=now + exp_offset,
        iat=now,
    )
    return encode_session_jwt(claims, secret=secret)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_valid_jwt_resolves_user_principal(
    client: httpx.AsyncClient, service: AuthService
) -> None:
    resp = await client.get("/auth/me", headers=_auth(_jwt(role=UserRole.ADMIN)))
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == str(USER_ID)
    assert body["workspace_id"] == str(WS)
    assert body["role"] == "admin"
    assert body["email"] == "alice@example.com"
    assert body["auth_method"] == "session_jwt"


async def test_expired_jwt_is_401(client: httpx.AsyncClient, service: AuthService) -> None:
    resp = await client.get("/auth/me", headers=_auth(_jwt(exp_offset=-60)))
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"


async def test_wrong_secret_jwt_is_401(client: httpx.AsyncClient, service: AuthService) -> None:
    resp = await client.get("/auth/me", headers=_auth(_jwt(secret="not-the-secret")))
    assert resp.status_code == 401


async def test_wrong_audience_jwt_is_401(
    client: httpx.AsyncClient, service: AuthService
) -> None:
    resp = await client.get("/auth/me", headers=_auth(_jwt(aud="somewhere-else")))
    assert resp.status_code == 401


async def test_jwt_rejected_when_auth_secret_unconfigured(
    client: httpx.AsyncClient,
) -> None:
    svc = AuthService(secret_key=b"3" * 32, auth_secret="")
    app.dependency_overrides[get_auth_service] = lambda: svc
    try:
        resp = await client.get("/auth/me", headers=_auth(_jwt()))
        assert resp.status_code == 401  # fail-closed, never a silent default
    finally:
        app.dependency_overrides.pop(get_auth_service, None)


async def test_jwt_role_gates_rbac(client: httpx.AsyncClient, service: AuthService) -> None:
    # A viewer session JWT cannot manage secrets (403), an admin one can.
    viewer = await client.get("/auth/secrets", headers=_auth(_jwt(role=UserRole.VIEWER)))
    assert viewer.status_code == 403
    admin = await client.get("/auth/secrets", headers=_auth(_jwt(role=UserRole.ADMIN)))
    assert admin.status_code == 200


async def test_api_key_auth_still_works(
    client: httpx.AsyncClient, service: AuthService
) -> None:
    _, token = service.bootstrap_key(workspace_id=WS, name="ci", role=UserRole.MEMBER)
    resp = await client.get("/auth/me", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["auth_method"] == "api_key"
