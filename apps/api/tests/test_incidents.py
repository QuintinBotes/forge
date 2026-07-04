"""Integration tests for the incident + alert routers (F17)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from forge_api.deps import Principal, get_current_principal
from forge_api.main import create_app
from forge_api.routers.incidents import IncidentServiceRegistry, get_incident_registry
from forge_api.settings import Settings, get_settings
from forge_contracts import UserRole
from forge_contracts.incident import AlertProvider
from forge_integrations.alerts import get_alert_adapter

TEST_WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
TEST_USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
PROJECT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c3")


def make_test_principal(
    *, role: UserRole = UserRole.MEMBER, workspace_id: uuid.UUID = TEST_WORKSPACE_ID
) -> Principal:
    return Principal(
        user_id=TEST_USER_ID,
        workspace_id=workspace_id,
        role=role,
        email="test-principal@forge.local",
        auth_method="test",
        scopes=["*"],
    )
WEBHOOK_SECRETS = {
    "pagerduty_webhook_secret": "pd-secret",
    "datadog_webhook_secret": "dd-secret",
    "sentry_webhook_secret": "sn-secret",
    "grafana_webhook_secret": "gf-secret",
}


@pytest.fixture
def registry() -> IncidentServiceRegistry:
    return IncidentServiceRegistry()


def _build(
    registry: IncidentServiceRegistry,
    *,
    role: UserRole = UserRole.MEMBER,
    with_secrets: bool = True,
) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_current_principal] = lambda: make_test_principal(role=role)
    app.dependency_overrides[get_incident_registry] = lambda: registry
    if with_secrets:
        app.dependency_overrides[get_settings] = lambda: Settings(**WEBHOOK_SECRETS)
    return TestClient(app)


@pytest.fixture
def client(registry: IncidentServiceRegistry) -> Iterator[TestClient]:
    with _build(registry) as c:
        yield c


def _declare(client: TestClient, title: str = "DB outage") -> dict:
    resp = client.post(
        "/incidents",
        json={"project_id": str(PROJECT_ID), "title": title, "severity": "high"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_declare_list_get_timeline(client: TestClient) -> None:
    inc = _declare(client)
    assert inc["lifecycle_state"] == "incident_created"
    assert inc["state"] == "incident_created"

    listed = client.get("/incidents").json()
    assert len(listed) == 1

    detail = client.get(f"/incidents/{inc['id']}").json()
    assert detail["event_count"] >= 1

    timeline = client.get(f"/incidents/{inc['id']}/timeline").json()
    assert any(ev["kind"] == "state_change" for ev in timeline)


def _drive(client: TestClient, incident_id: str, event: str) -> dict:
    resp = client.post(f"/incidents/{incident_id}/events", json={"event": event})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_full_happy_path_to_postmortem(client: TestClient) -> None:
    inc = _declare(client)
    iid = inc["id"]
    _drive(client, iid, "incident_acknowledged")
    _drive(client, iid, "context_gathered")
    _drive(client, iid, "impact_assessed")
    # Propose a clean, low-blast remediation.
    plan = client.post(
        f"/incidents/{iid}/remediation",
        json={"steps": [{"id": "s1", "order": 1, "title": "tail logs", "action": "read_logs"}]},
    )
    assert plan.status_code == 200, plan.text
    assert plan.json()["offending_step_ids"] == []
    _drive(client, iid, "remediation_proposed")
    detail = _drive(client, iid, "remediation_approved")
    assert detail["lifecycle_state"] == "executing_runbook"
    _drive(client, iid, "runbook_completed")
    _drive(client, iid, "recovery_confirmed")
    pm_state = _drive(client, iid, "postmortem_requested")
    assert pm_state["lifecycle_state"] == "postmortem_created"
    closed = _drive(client, iid, "close")
    assert closed["lifecycle_state"] == "closed"

    pm = client.get(f"/incidents/{iid}/postmortem").json()
    assert pm["content_md"]
    assert len(pm["action_item_task_keys"]) >= 1


def test_blast_radius_exceeded_routes_to_needs_human_input(client: TestClient) -> None:
    inc = _declare(client)
    iid = inc["id"]
    _drive(client, iid, "incident_acknowledged")
    _drive(client, iid, "context_gathered")
    _drive(client, iid, "impact_assessed")
    plan = client.post(
        f"/incidents/{iid}/remediation",
        json={"steps": [{"id": "bad", "order": 1, "title": "deploy", "action": "deploy_prod"}]},
    ).json()
    assert plan["offending_step_ids"] == ["bad"]
    # The within edge cannot fire; the exceeded edge routes to needs_human_input.
    bad = client.post(f"/incidents/{iid}/events", json={"event": "remediation_proposed"})
    assert bad.status_code == 409
    esc = _drive(client, iid, "remediation_blast_radius_exceeded")
    assert esc["lifecycle_state"] == "needs_human_input"


def test_close_blocked_until_postmortem(client: TestClient) -> None:
    inc = _declare(client)
    iid = inc["id"]
    # Cannot close straight from incident_created.
    resp = client.post(f"/incidents/{iid}/events", json={"event": "close"})
    assert resp.status_code == 409


def test_viewer_cannot_declare_or_drive(registry: IncidentServiceRegistry) -> None:
    member = _build(registry, role=UserRole.MEMBER)
    inc = _declare(member)
    viewer = _build(registry, role=UserRole.VIEWER)
    # Viewer can read.
    assert viewer.get(f"/incidents/{inc['id']}").status_code == 200
    # Viewer cannot declare or drive.
    assert viewer.post(
        "/incidents", json={"project_id": str(PROJECT_ID), "title": "x"}
    ).status_code == 403
    assert viewer.post(
        f"/incidents/{inc['id']}/events", json={"event": "incident_acknowledged"}
    ).status_code == 403


def test_agent_runner_cannot_approve(registry: IncidentServiceRegistry) -> None:
    member = _build(registry, role=UserRole.MEMBER)
    inc = _declare(member)
    iid = inc["id"]
    for ev in ("incident_acknowledged", "context_gathered", "impact_assessed"):
        _drive(member, iid, ev)
    member.post(
        f"/incidents/{iid}/remediation",
        json={"steps": [{"id": "s1", "order": 1, "title": "logs", "action": "read_logs"}]},
    )
    _drive(member, iid, "remediation_proposed")
    # The agent-runner (lacks WRITE) cannot approve its own remediation.
    agent = _build(registry, role=UserRole.AGENT_RUNNER)
    resp = agent.post(f"/incidents/{iid}/events", json={"event": "remediation_approved"})
    assert resp.status_code == 403


def test_cross_workspace_404(registry: IncidentServiceRegistry) -> None:
    ws_a = make_test_principal(workspace_id=uuid.uuid4())
    ws_b = make_test_principal(workspace_id=uuid.uuid4())

    app_a = create_app()
    app_a.dependency_overrides[get_current_principal] = lambda: ws_a
    app_a.dependency_overrides[get_incident_registry] = lambda: registry
    client_a = TestClient(app_a)
    inc = _declare(client_a)

    app_b = create_app()
    app_b.dependency_overrides[get_current_principal] = lambda: ws_b
    app_b.dependency_overrides[get_incident_registry] = lambda: registry
    client_b = TestClient(app_b)
    assert client_b.get(f"/incidents/{inc['id']}").status_code == 404


# --------------------------------------------------------------------------- #
# Alert webhooks                                                               #
# --------------------------------------------------------------------------- #

WS = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


def _webhook_url(provider: str) -> str:
    return f"/integrations/alerts/{provider}/webhook?workspace_id={WS}&project_id={PROJECT_ID}"


def _signed(provider: AlertProvider, body: bytes, secret: str) -> dict[str, str]:
    adapter = get_alert_adapter(provider)
    return {adapter.signature_header: adapter.sign(secret, body)}


def test_webhook_bad_signature_401(client: TestClient) -> None:
    body = b'{"id":"1","title":"x","priority":"P1","aggreg_key":"k"}'
    resp = client.post(
        _webhook_url("datadog"), content=body,
        headers={"X-Datadog-Signature": "deadbeef"},
    )
    assert resp.status_code == 401


def test_webhook_missing_secret_501(registry: IncidentServiceRegistry) -> None:
    no_secret = _build(registry, with_secrets=False)
    body = b'{"id":"1","title":"x","priority":"P1","aggreg_key":"k"}'
    resp = no_secret.post(
        _webhook_url("datadog"), content=body,
        headers=_signed(AlertProvider.DATADOG, body, "dd-secret"),
    )
    assert resp.status_code == 501


def test_webhook_unknown_provider_404(client: TestClient) -> None:
    resp = client.post(_webhook_url("nagios"), content=b"{}")
    assert resp.status_code == 404


def test_webhook_creates_then_dedup_attaches_then_idempotent(client: TestClient) -> None:
    body = b'{"id":"e1","title":"High CPU","priority":"P1","aggreg_key":"svc:cpu"}'
    headers = {
        **_signed(AlertProvider.DATADOG, body, "dd-secret"),
        "X-Datadog-Delivery-Id": "del-1",
    }
    first = client.post(_webhook_url("datadog"), content=body, headers=headers)
    assert first.status_code == 202, first.text
    assert first.json()["status"] == "created_incident"

    # Same delivery id -> skipped idempotently, no new incident.
    again = client.post(_webhook_url("datadog"), content=body, headers=headers)
    assert again.status_code == 202
    assert again.json()["status"] == "skipped"

    # Different delivery, same dedup key, incident still open -> attached.
    headers2 = {
        **_signed(AlertProvider.DATADOG, body, "dd-secret"),
        "X-Datadog-Delivery-Id": "del-2",
    }
    attach = client.post(_webhook_url("datadog"), content=body, headers=headers2)
    assert attach.status_code == 202
    assert attach.json()["status"] == "attached"

    # Only one incident exists in the workspace.
    listed = client.get("/incidents").json()
    assert len(listed) == 1


def test_manual_alert_creates_incident(client: TestClient) -> None:
    resp = client.post(
        "/integrations/alerts/manual",
        json={"project_id": str(PROJECT_ID), "title": "manual incident", "severity": "high"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["source"] == "manual"
