"""Tests for the /auth/* API routes (Task 1.15 — auth & secrets).

Exercises API-key auth round-trip, the BYOK secret endpoints, RBAC enforcement,
and secret redaction over the real ASGI app with an isolated AuthService.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import httpx
import pytest

from forge_api.auth.service import AuthService, get_auth_service
from forge_api.main import app
from forge_contracts.enums import UserRole

WS = uuid.uuid4()


@pytest.fixture
def service() -> Iterator[AuthService]:
    svc = AuthService(secret_key=b"1" * 32)
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


def _admin_token(service: AuthService) -> str:
    _, token = service.bootstrap_key(workspace_id=WS, name="admin", role=UserRole.ADMIN)
    return token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_me_requires_authentication(
    client: httpx.AsyncClient, service: AuthService
) -> None:
    resp = await client.get("/auth/me")
    assert resp.status_code == 401


async def test_me_returns_principal_for_valid_key(
    client: httpx.AsyncClient, service: AuthService
) -> None:
    token = _admin_token(service)
    resp = await client.get("/auth/me", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["workspace_id"] == str(WS)
    assert body["role"] == "admin"
    assert body["auth_method"] == "api_key"


async def test_api_key_create_and_list_roundtrip(
    client: httpx.AsyncClient, service: AuthService
) -> None:
    token = _admin_token(service)
    create = await client.post(
        "/auth/api-keys",
        headers=_auth(token),
        json={"name": "ci-runner", "role": "agent-runner"},
    )
    assert create.status_code == 201
    created = create.json()
    minted = created["token"]
    assert minted.startswith("forge_")

    # The minted token actually authenticates.
    me = await client.get("/auth/me", headers=_auth(minted))
    assert me.status_code == 200
    assert me.json()["role"] == "agent-runner"

    listed = await client.get("/auth/api-keys", headers=_auth(token))
    assert listed.status_code == 200
    names = {k["name"] for k in listed.json()}
    assert {"admin", "ci-runner"} <= names
    # Listing must never echo a usable token.
    assert minted not in listed.text


async def test_non_admin_cannot_create_api_keys(
    client: httpx.AsyncClient, service: AuthService
) -> None:
    _, viewer_token = service.bootstrap_key(
        workspace_id=WS, name="viewer", role=UserRole.VIEWER
    )
    resp = await client.post(
        "/auth/api-keys",
        headers=_auth(viewer_token),
        json={"name": "x", "role": "member"},
    )
    assert resp.status_code == 403


async def test_secret_create_returns_no_plaintext_and_is_decryptable(
    client: httpx.AsyncClient, service: AuthService
) -> None:
    token = _admin_token(service)
    resp = await client.post(
        "/auth/secrets",
        headers=_auth(token),
        json={
            "name": "anthropic",
            "secret": "sk-ant-PLAINTEXTSECRET99",
            "kind": "model_provider",
            "provider": "anthropic",
        },
    )
    assert resp.status_code == 201
    assert "sk-ant-PLAINTEXTSECRET99" not in resp.text
    secret_id = resp.json()["id"]

    # Stored encrypted but decryptable server-side within the same workspace.
    assert service.vault.get_secret(WS, uuid.UUID(secret_id)) == "sk-ant-PLAINTEXTSECRET99"

    listed = await client.get("/auth/secrets", headers=_auth(token))
    assert listed.status_code == 200
    assert "sk-ant-PLAINTEXTSECRET99" not in listed.text


async def test_viewer_cannot_write_secret(
    client: httpx.AsyncClient, service: AuthService
) -> None:
    _, viewer_token = service.bootstrap_key(
        workspace_id=WS, name="viewer", role=UserRole.VIEWER
    )
    resp = await client.post(
        "/auth/secrets",
        headers=_auth(viewer_token),
        json={"name": "x", "secret": "y", "kind": "system"},
    )
    assert resp.status_code == 403


async def test_invalid_token_rejected(
    client: httpx.AsyncClient, service: AuthService
) -> None:
    resp = await client.get("/auth/me", headers=_auth("forge_system_not_a_real_token"))
    assert resp.status_code == 401


async def test_login_describes_oauth_providers(
    client: httpx.AsyncClient, service: AuthService
) -> None:
    resp = await client.post("/auth/login", json={"provider": "github"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "github"
    assert "authorize_url" in body
