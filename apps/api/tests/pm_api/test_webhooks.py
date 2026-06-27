"""PM webhook intake: signature/secret verification + dedup (AC15, AC16, AC17, AC19)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from forge_integrations.pm.linear.webhooks import sign_linear

from .conftest import WORKSPACE_ID


def _create(client: TestClient, project_id, provider: str, **overrides) -> dict:
    payload = {
        "provider": provider,
        "name": f"{provider} conn",
        "project_id": str(project_id),
        "external_project_key": "ENG",
        "auth_type": "api_token",
        "api_token": "tok",
        **overrides,
    }
    resp = client.post("/integrations/pm/connections", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _webhook_secret(pm_service, connection_id: uuid.UUID, vault) -> str:
    conn = pm_service.get(WORKSPACE_ID, connection_id)
    return vault.get_secret(WORKSPACE_ID, uuid.UUID(conn.webhook_secret_ref))


# --- Linear HMAC (AC15) ----------------------------------------------------- #

def test_webhook_linear_signed_202(client, project_id, pm_service, vault) -> None:
    conn = _create(client, project_id, "linear")
    secret = _webhook_secret(pm_service, uuid.UUID(conn["id"]), vault)
    ts = int(datetime.now(UTC).timestamp() * 1000)
    body = json.dumps(
        {"action": "update", "type": "Issue", "webhookTimestamp": ts,
         "webhookId": "wh-1", "data": {"id": "uuid-1", "identifier": "ENG-1"}}
    ).encode()
    sig = sign_linear(secret, body)
    resp = client.post(
        f"/integrations/pm/webhooks/linear/{conn['id']}",
        content=body,
        headers={"Linear-Signature": sig, "Content-Type": "application/json"},
    )
    assert resp.status_code == 202, resp.text


def test_webhook_linear_unsigned_401(client, project_id) -> None:
    conn = _create(client, project_id, "linear")
    resp = client.post(
        f"/integrations/pm/webhooks/linear/{conn['id']}",
        content=b'{"action":"update"}',
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 401


def test_webhook_linear_bad_signature_401(client, project_id) -> None:
    conn = _create(client, project_id, "linear")
    resp = client.post(
        f"/integrations/pm/webhooks/linear/{conn['id']}",
        content=b'{"action":"update","webhookTimestamp":1}',
        headers={"Linear-Signature": "deadbeef", "Content-Type": "application/json"},
    )
    assert resp.status_code == 401


# --- Jira secret (AC16) ----------------------------------------------------- #

def test_webhook_jira_secret_202(client, project_id, pm_service, vault) -> None:
    conn = _create(client, project_id, "jira", external_base_url="https://acme.atlassian.net")
    secret = _webhook_secret(pm_service, uuid.UUID(conn["id"]), vault)
    body = json.dumps(
        {"webhookEvent": "jira:issue_updated", "timestamp": 1767348000000,
         "issue": {"id": "10001", "key": "ENG-1"}}
    ).encode()
    resp = client.post(
        f"/integrations/pm/webhooks/jira/{conn['id']}",
        content=body,
        headers={"X-Forge-PM-Secret": secret, "Content-Type": "application/json"},
    )
    assert resp.status_code == 202, resp.text


def test_webhook_jira_wrong_secret_401(client, project_id) -> None:
    conn = _create(client, project_id, "jira", external_base_url="https://acme.atlassian.net")
    body = b'{"webhookEvent":"jira:issue_updated","issue":{"id":"10001","key":"ENG-1"}}'
    resp = client.post(
        f"/integrations/pm/webhooks/jira/{conn['id']}",
        content=body,
        headers={"X-Forge-PM-Secret": "wrong", "Content-Type": "application/json"},
    )
    assert resp.status_code == 401


# --- Idempotency (AC17) ----------------------------------------------------- #

def test_webhook_dedup_one_row(client, project_id, pm_service, vault, session_factory) -> None:
    conn = _create(client, project_id, "linear")
    secret = _webhook_secret(pm_service, uuid.UUID(conn["id"]), vault)
    ts = int(datetime.now(UTC).timestamp() * 1000)
    body = json.dumps(
        {"action": "update", "type": "Issue", "webhookTimestamp": ts,
         "webhookId": "wh-1", "data": {"id": "uuid-1", "identifier": "ENG-1"}}
    ).encode()
    sig = sign_linear(secret, body)
    headers = {"Linear-Signature": sig, "Content-Type": "application/json"}
    url = f"/integrations/pm/webhooks/linear/{conn['id']}"

    assert client.post(url, content=body, headers=headers).status_code == 202
    assert client.post(url, content=body, headers=headers).status_code == 202

    from forge_db.models.pm import PMWebhookDelivery

    with session_factory() as session:
        rows = session.query(PMWebhookDelivery).all()
        assert len(rows) == 1


# --- Direction enforcement (AC19) ------------------------------------------ #

def test_outbound_only_records_skipped(
    client, project_id, pm_service, vault, session_factory
) -> None:
    conn = _create(client, project_id, "linear", sync_direction="outbound_only")
    secret = _webhook_secret(pm_service, uuid.UUID(conn["id"]), vault)
    ts = int(datetime.now(UTC).timestamp() * 1000)
    body = json.dumps(
        {"action": "update", "type": "Issue", "webhookTimestamp": ts,
         "webhookId": "wh-9", "data": {"id": "uuid-9", "identifier": "ENG-9"}}
    ).encode()
    sig = sign_linear(secret, body)
    resp = client.post(
        f"/integrations/pm/webhooks/linear/{conn['id']}",
        content=body,
        headers={"Linear-Signature": sig, "Content-Type": "application/json"},
    )
    assert resp.status_code == 202

    from forge_db.models.enums import PMDeliveryStatus
    from forge_db.models.pm import PMWebhookDelivery

    with session_factory() as session:
        row = session.query(PMWebhookDelivery).one()
        assert row.status == PMDeliveryStatus.SKIPPED


def test_webhook_unknown_connection_404(client) -> None:
    resp = client.post(
        f"/integrations/pm/webhooks/linear/{uuid.uuid4()}",
        content=b'{"action":"update","webhookTimestamp":1}',
        headers={"Linear-Signature": "x"},
    )
    assert resp.status_code in (401, 404)
