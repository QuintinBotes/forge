"""Spec M at the API boundary: GitHub Issues connections, webhook route, dispatch config.

Covers the M2/M3 API surface: creating a ``github_issues`` connection
(external_wins by default), the ``POST /integrations/pm/webhooks/github_issues/{id}``
HMAC intake (``x-hub-signature-256``), forge-marker echo suppression at
intake, replay dedupe, and ``dispatch`` config validation on create + patch.
"""

from __future__ import annotations

import json
import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from forge_db.models import Project
from forge_db.models.enums import PMDeliveryStatus, PMProvider
from forge_db.models.pm import PMWebhookDelivery
from forge_integrations import sign_github_payload

from .conftest import SDK_FIXTURES, WORKSPACE_ID


def _fixture(name: str) -> dict:
    return json.loads((SDK_FIXTURES / "github_issues" / name).read_text())


def _create_github(client: TestClient, project_id: uuid.UUID, **overrides) -> dict:
    payload = {
        "provider": "github_issues",
        "name": "GitHub Issues",
        "project_id": str(project_id),
        "external_project_key": "acme/widgets",
        "auth_type": "api_token",
        "api_token": "tok",
        **overrides,
    }
    resp = client.post("/integrations/pm/connections", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _secret(pm_service, vault, connection_id: str) -> str:
    conn = pm_service.get(WORKSPACE_ID, uuid.UUID(connection_id))
    return vault.get_secret(WORKSPACE_ID, uuid.UUID(conn.webhook_secret_ref))


def _post_webhook(client: TestClient, connection_id: str, body: bytes, secret: str, event: str):
    return client.post(
        f"/integrations/pm/webhooks/github_issues/{connection_id}",
        content=body,
        headers={
            "X-Hub-Signature-256": sign_github_payload(secret, body),
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": "delivery-42",
            "Content-Type": "application/json",
        },
    )


def test_create_github_issues_connection_defaults_tracker_wins(client, project_id) -> None:
    conn = _create_github(client, project_id)
    assert conn["provider"] == "github_issues"
    # Spec M M2: new connections default to external_wins.
    assert conn["conflict_policy"] == "external_wins"
    assert conn["dispatch"] == {
        "enabled": False,
        "label": "forge:run",
        "repository": None,
        "allowed_senders": [],
        "max_active_runs": 2,
        "status_on_pr_opened": None,
    }


def test_github_webhook_signed_202_and_replay_deduped(
    client, project_id, pm_service, vault, session_factory, enqueued
) -> None:
    conn = _create_github(client, project_id)
    secret = _secret(pm_service, vault, conn["id"])
    body = json.dumps(_fixture("issue_labeled.json")).encode()

    assert _post_webhook(client, conn["id"], body, secret, "issues").status_code == 202
    assert _post_webhook(client, conn["id"], body, secret, "issues").status_code == 202

    with session_factory() as session:
        rows = session.query(PMWebhookDelivery).all()
        assert len(rows) == 1  # the replay is deduped, never enqueued twice
        assert rows[0].provider == PMProvider.GITHUB_ISSUES
        assert rows[0].status == PMDeliveryStatus.RECEIVED
    assert enqueued == [rows[0].id]


def test_github_webhook_unsigned_and_forged_401(client, project_id, pm_service, vault) -> None:
    conn = _create_github(client, project_id)
    body = json.dumps(_fixture("issue_labeled.json")).encode()
    url = f"/integrations/pm/webhooks/github_issues/{conn['id']}"
    assert client.post(url, content=body).status_code == 401
    assert (
        client.post(url, content=body, headers={"X-Hub-Signature-256": "sha256=bad"}).status_code
        == 401
    )


def test_forge_marker_comment_echo_suppressed_at_intake(
    client, project_id, pm_service, vault, session_factory, enqueued
) -> None:
    conn = _create_github(client, project_id)
    secret = _secret(pm_service, vault, conn["id"])
    payload = _fixture("issue_comment.json")
    payload["comment"]["body"] = (
        "Forge run finished.\n\n<!-- forge:run:12345678-1234-1234-1234-1234567890ab -->"
    )
    body = json.dumps(payload).encode()

    resp = _post_webhook(client, conn["id"], body, secret, "issue_comment")
    assert resp.status_code == 202
    with session_factory() as session:
        row = session.query(PMWebhookDelivery).one()
        assert row.status == PMDeliveryStatus.ECHO_SUPPRESSED
    assert enqueued == []


def test_patch_dispatch_config_round_trips(client, project_id) -> None:
    conn = _create_github(client, project_id)
    resp = client.patch(
        f"/integrations/pm/connections/{conn['id']}",
        json={
            "dispatch": {
                "enabled": True,
                "repository": "acme/widgets",
                "allowed_senders": ["octocat"],
                "status_on_pr_opened": "started",
            }
        },
    )
    assert resp.status_code == 200, resp.text
    dispatch = resp.json()["dispatch"]
    assert dispatch["enabled"] is True
    assert dispatch["label"] == "forge:run"
    assert dispatch["repository"] == "acme/widgets"
    assert dispatch["allowed_senders"] == ["octocat"]
    assert dispatch["status_on_pr_opened"] == "started"

    fetched = client.get(f"/integrations/pm/connections/{conn['id']}")
    assert fetched.json()["dispatch"] == dispatch


def test_patch_dispatch_config_rejects_invalid(client, project_id) -> None:
    conn = _create_github(client, project_id)
    url = f"/integrations/pm/connections/{conn['id']}"
    for bad in (
        {"dispatch": {"max_active_runs": 0}},
        {"dispatch": {"max_active_runs": 11}},
        {"dispatch": {"repository": "noslash"}},
        {"dispatch": {"bogus_key": True}},
        {"dispatch": {"status_on_pr_opened": "doing"}},
    ):
        assert client.patch(url, json=bad).status_code == 422, bad


def test_create_with_invalid_dispatch_is_422(
    client, session_factory: sessionmaker[Session]
) -> None:
    with session_factory() as session:
        project = Project(workspace_id=WORKSPACE_ID, name="Second", key="SECOND")
        session.add(project)
        session.commit()
        project_id = project.id
    resp = client.post(
        "/integrations/pm/connections",
        json={
            "provider": "github_issues",
            "name": "bad dispatch",
            "project_id": str(project_id),
            "external_project_key": "acme/widgets",
            "dispatch": {"max_active_runs": 99},
        },
    )
    assert resp.status_code == 422
