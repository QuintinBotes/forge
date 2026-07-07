"""Router tests for BYOK secret rotation, expiry badge, and 409 (HARD-13 AC8)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from forge_api.auth.service import AuthService, get_auth_service
from forge_api.main import app
from forge_contracts.enums import APIKeyKind, UserRole

WS = uuid.uuid4()


@pytest.fixture
def service() -> Iterator[AuthService]:
    svc = AuthService(secret_key=b"2" * 32)
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


def _admin_auth(service: AuthService) -> dict[str, str]:
    _, token = service.bootstrap_key(workspace_id=WS, name="admin", role=UserRole.ADMIN)
    return {"Authorization": f"Bearer {token}"}


async def test_rotate_secret_changes_value(client: httpx.AsyncClient, service: AuthService) -> None:
    auth = _admin_auth(service)
    created = await client.post(
        "/auth/secrets",
        json={"name": "gh", "secret": "ghp_OLD000000000000000", "kind": "integration_token"},
        headers=auth,
    )
    assert created.status_code == 201
    secret_id = created.json()["id"]

    resp = await client.post(
        f"/auth/secrets/{secret_id}/rotate",
        json={"secret": "ghp_NEW111111111111111"},
        headers=auth,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == secret_id
    # Server-side, the new value decrypts; plaintext never crosses the wire.
    assert service.vault.get_secret(WS, uuid.UUID(secret_id)) == "ghp_NEW111111111111111"
    assert "ghp_NEW111111111111111" not in resp.text


async def test_list_secrets_exposes_is_expired_badge(
    client: httpx.AsyncClient, service: AuthService
) -> None:
    auth = _admin_auth(service)
    service.vault.put_secret(
        workspace_id=WS,
        name="expired-key",
        secret="sk-EXPIRED0000000000",
        kind=APIKeyKind.MODEL_PROVIDER,
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    resp = await client.get("/auth/secrets", headers=auth)
    assert resp.status_code == 200
    badge = {s["name"]: s["is_expired"] for s in resp.json()}
    assert badge["expired-key"] is True


async def test_check_expired_secret_returns_409(
    client: httpx.AsyncClient, service: AuthService
) -> None:
    auth = _admin_auth(service)
    info = service.vault.put_secret(
        workspace_id=WS,
        name="anthropic",
        secret="sk-ant-EXPIRED000000",
        kind=APIKeyKind.MODEL_PROVIDER,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    resp = await client.post(f"/auth/secrets/{info.id}/check", headers=auth)
    assert resp.status_code == 409
    assert "sk-ant-EXPIRED000000" not in resp.text


async def test_check_valid_secret_returns_ok(
    client: httpx.AsyncClient, service: AuthService
) -> None:
    auth = _admin_auth(service)
    info = service.vault.put_secret(
        workspace_id=WS,
        name="valid",
        secret="sk-VALID0000000000000",
        kind=APIKeyKind.MODEL_PROVIDER,
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    resp = await client.post(f"/auth/secrets/{info.id}/check", headers=auth)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


async def test_rotate_requires_secret_manager_permission(
    client: httpx.AsyncClient, service: AuthService
) -> None:
    _, token = service.bootstrap_key(workspace_id=WS, name="viewer", role=UserRole.VIEWER)
    resp = await client.post(
        f"/auth/secrets/{uuid.uuid4()}/rotate",
        json={"secret": "x"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
