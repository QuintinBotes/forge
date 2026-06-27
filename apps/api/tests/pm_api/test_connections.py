"""PM connection API: create/list/get/patch/disconnect/test (AC2, AC3, AC21, AC24)."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from .conftest import WORKSPACE_ID


def _payload(project_id: uuid.UUID, **overrides) -> dict:
    base = {
        "provider": "linear",
        "name": "Eng Linear",
        "project_id": str(project_id),
        "external_project_key": "ENG",
        "auth_type": "api_token",
        "api_token": "super-secret-token",
    }
    base.update(overrides)
    return base


def test_create_connection_stores_token_in_vault_not_response(
    client: TestClient, project_id, vault
) -> None:
    resp = client.post("/integrations/pm/connections", json=_payload(project_id))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # Token must never be echoed anywhere in the response.
    assert "super-secret-token" not in resp.text
    assert "api_token" not in body
    assert body["has_credential"] is True
    assert body["has_webhook_secret"] is True
    assert body["status"] == "pending"


def test_create_connection_prefills_maps(client: TestClient, project_id) -> None:
    resp = client.post("/integrations/pm/connections", json=_payload(project_id))
    body = resp.json()
    # Linear status map defaults to the category 1:1 table.
    assert body["status_map"]["started"] == "started"
    assert body["priority_map"]["urgent"] == "1"


def test_create_duplicate_returns_409(client: TestClient, project_id) -> None:
    assert client.post("/integrations/pm/connections", json=_payload(project_id)).status_code == 201
    dupe = client.post("/integrations/pm/connections", json=_payload(project_id))
    assert dupe.status_code == 409


def test_list_and_get_connection(client: TestClient, project_id) -> None:
    created = client.post("/integrations/pm/connections", json=_payload(project_id)).json()
    listing = client.get("/integrations/pm/connections")
    assert listing.status_code == 200
    assert any(c["id"] == created["id"] for c in listing.json())

    detail = client.get(f"/integrations/pm/connections/{created['id']}")
    assert detail.status_code == 200
    assert detail.json()["link_counts"] == {}


def test_get_cross_workspace_404(client_factory, project_id) -> None:
    admin = client_factory()
    created = admin.post("/integrations/pm/connections", json=_payload(project_id)).json()
    from forge_contracts import UserRole

    from .conftest import OTHER_WORKSPACE_ID

    other = client_factory(role=UserRole.ADMIN, workspace_id=OTHER_WORKSPACE_ID)
    assert other.get(f"/integrations/pm/connections/{created['id']}").status_code == 404


def test_patch_enable_disable(client: TestClient, project_id) -> None:
    created = client.post("/integrations/pm/connections", json=_payload(project_id)).json()
    patched = client.patch(
        f"/integrations/pm/connections/{created['id']}",
        json={"name": "Renamed", "enabled": True},
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "Renamed"
    assert patched.json()["status"] == "connected"


def test_disconnect_sets_disabled(client: TestClient, project_id) -> None:
    created = client.post("/integrations/pm/connections", json=_payload(project_id)).json()
    resp = client.delete(f"/integrations/pm/connections/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "disabled"


def test_test_endpoint_persists_health(client: TestClient, project_id, vault) -> None:
    created = client.post("/integrations/pm/connections", json=_payload(project_id)).json()
    resp = client.post(f"/integrations/pm/connections/{created['id']}/test")
    assert resp.status_code == 200, resp.text
    health = resp.json()
    assert health["status"] == "connected"
    assert health["account"] == "me@acme.test"
    # Persisted on the connection.
    detail = client.get(f"/integrations/pm/connections/{created['id']}").json()
    assert detail["status"] == "connected"
    assert detail["last_health_at"] is not None


def test_secret_never_in_vault_listed_plaintext(client: TestClient, project_id, vault) -> None:
    client.post("/integrations/pm/connections", json=_payload(project_id))
    for info in vault.list_secrets(WORKSPACE_ID):
        # SecretInfo is redaction-safe: no plaintext attribute exists.
        assert "super-secret-token" not in repr(info)
