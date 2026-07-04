"""Integration tests for the integration router (Phase 2 Task 2.1 wires ``/integration/*``).

Exercises the real handlers. GitHub/Slack clients are driven by an
``httpx.MockTransport`` (no live calls) via dependency override; the webhook
parser is fully offline.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forge_api.main import create_app
from forge_api.routers.integration import (
    get_github_client,
    get_github_client_optional,
    get_github_webhook_secret,
    get_slack_notifier,
)
from forge_integrations import GitHubClient, SlackNotifier, sign_github_payload

WEBHOOK_SECRET = "whsec_test_123"


@pytest.fixture
def client(authenticate_app: Callable[..., FastAPI]) -> Iterator[TestClient]:
    app = create_app()
    authenticate_app(app)
    app.dependency_overrides[get_github_webhook_secret] = lambda: WEBHOOK_SECRET

    def github_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls"):
            return httpx.Response(
                201,
                json={
                    "number": 42,
                    "html_url": "https://github.com/org/api/pull/42",
                    "state": "open",
                    "title": "t",
                    "head": {"ref": "feature", "sha": "abc"},
                    "base": {"ref": "main"},
                },
            )
        return httpx.Response(404, json={"message": "not found"})

    def slack_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "channel": "C1", "ts": "1.2"})

    gh = GitHubClient(token="t", transport=httpx.MockTransport(github_handler))
    slack = SlackNotifier(token="t", transport=httpx.MockTransport(slack_handler))
    app.dependency_overrides[get_github_client] = lambda: gh
    app.dependency_overrides[get_slack_notifier] = lambda: slack
    with TestClient(app) as c:
        yield c


def test_open_pr(client: TestClient) -> None:
    resp = client.post(
        "/integration/github/pull-requests",
        json={"repo": "org/api", "title": "t", "head": "feature", "base": "main"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["number"] == 42
    assert body["state"] == "open"


def test_slack_notify(client: TestClient) -> None:
    resp = client.post(
        "/integration/slack/notify",
        json={"channel": "C1", "text": "hello"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True


def _slack_principal(ws_id):
    """A self-contained authenticated principal (avoids the shadowed bare
    ``conftest`` name under the full-suite run)."""
    import uuid as _uuid

    from forge_api.deps import Principal
    from forge_contracts import UserRole

    return Principal(
        user_id=_uuid.UUID("00000000-0000-0000-0000-0000000000e8"),
        workspace_id=ws_id,
        role=UserRole.ADMIN,
        email="slack-router-test@forge.local",
        auth_method="test",
        scopes=["*"],
    )


def test_slack_notify_approval_records_ts(authenticate_app: Callable[..., FastAPI]) -> None:
    """POST /slack/approvals/{id}/notify posts the gate + stashes channel/ts (AC)."""
    import uuid

    from forge_api.routers.approval import ApprovalStore, get_approval_store
    from forge_api.routers.integration import (
        SlackApprovalRefStore,
        get_slack_notifier,
        get_slack_ref_store,
    )
    from forge_contracts import ApprovalGate, ApprovalRequest

    ws_id = uuid.UUID("00000000-0000-0000-0000-0000000000f9")
    store = ApprovalStore()
    req = ApprovalRequest(id=uuid.uuid4(), gate=ApprovalGate.PR, title="Ship it")
    store.create(req, workspace_id=ws_id)
    refs = SlackApprovalRefStore()

    def slack_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "channel": "C42", "ts": "1700.500"})

    slack = SlackNotifier(token="t", transport=httpx.MockTransport(slack_handler))

    app = create_app()
    authenticate_app(app, _slack_principal(ws_id))
    app.dependency_overrides[get_approval_store] = lambda: store
    app.dependency_overrides[get_slack_notifier] = lambda: slack
    app.dependency_overrides[get_slack_ref_store] = lambda: refs
    with TestClient(app) as c:
        resp = c.post(f"/integration/slack/approvals/{req.id}/notify")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert refs.get(req.id) == ("C42", "1700.500")


def test_slack_notify_approval_404_for_unknown_gate(
    authenticate_app: Callable[..., FastAPI],
) -> None:
    import uuid

    from forge_api.routers.approval import ApprovalStore, get_approval_store
    from forge_api.routers.integration import get_slack_notifier

    store = ApprovalStore()
    slack = SlackNotifier(
        token="t", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))
    )
    app = create_app()
    authenticate_app(app)
    app.dependency_overrides[get_approval_store] = lambda: store
    app.dependency_overrides[get_slack_notifier] = lambda: slack
    with TestClient(app) as c:
        resp = c.post(f"/integration/slack/approvals/{uuid.uuid4()}/notify")
    assert resp.status_code == 404, resp.text


def _webhook_body() -> bytes:
    return json.dumps(
        {
            "source": "github",
            "event_type": "status",
            "payload": {
                "repository": {"full_name": "org/api"},
                "sha": "deadbeef",
                "state": "success",
                "context": "ci/build",
            },
        }
    ).encode()


def _post_webhook(client: TestClient, body: bytes, signature: str | None) -> httpx.Response:
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["X-Hub-Signature-256"] = signature
    return client.post("/integration/github/webhooks", content=body, headers=headers)


def test_github_webhook_parses_status_event_with_valid_signature(
    client: TestClient,
) -> None:
    body = _webhook_body()
    resp = _post_webhook(client, body, sign_github_payload(WEBHOOK_SECRET, body))
    assert resp.status_code == 200, resp.text
    parsed = resp.json()
    assert parsed["repo"] == "org/api"
    assert parsed["state"] == "success"
    assert parsed["sha"] == "deadbeef"


def test_github_webhook_rejects_missing_signature(client: TestClient) -> None:
    resp = _post_webhook(client, _webhook_body(), signature=None)
    assert resp.status_code == 401


def test_github_webhook_rejects_invalid_signature(client: TestClient) -> None:
    body = _webhook_body()
    # Signature computed with the wrong secret must not verify.
    resp = _post_webhook(client, body, sign_github_payload("wrong-secret", body))
    assert resp.status_code == 401


def test_github_webhook_rejects_tampered_body(client: TestClient) -> None:
    # Sign the original body, then deliver a tampered payload under that signature.
    signature = sign_github_payload(WEBHOOK_SECRET, _webhook_body())
    tampered = _webhook_body().replace(b"success", b"failure")
    resp = _post_webhook(client, tampered, signature)
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# HARD-01: App-auth wiring, fail-closed writes, health route, audit redaction  #
# --------------------------------------------------------------------------- #


def test_github_client_dep_uses_app_creds_when_configured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """When App creds are configured, the DI builds an App-auth client."""
    import forge_api.routers.integration as integ
    from forge_api.settings import Settings

    pem = tmp_path / "app.pem"
    # A syntactically-valid throwaway RSA key so from_app constructs cleanly.
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    fake = Settings(
        github_app_id="123",
        github_installation_id="456",
        github_app_private_key_path=str(pem),
    )
    monkeypatch.setattr(integ, "get_settings", lambda: fake)
    integ._github_client_singleton.cache_clear()
    try:
        client = integ._github_client_singleton()
        assert client is not None
        # The App path installs a per-request token provider (no static bearer).
        assert client._token_provider is not None
        assert "Authorization" not in client._client.headers
    finally:
        integ._github_client_singleton.cache_clear()


def test_write_route_501_when_no_github_creds(
    authenticate_app: Callable[..., FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With neither App creds nor a token, write routes fail closed (501)."""
    import forge_api.routers.integration as integ
    from forge_api.settings import Settings

    monkeypatch.setattr(integ, "get_settings", lambda: Settings())
    integ._github_client_singleton.cache_clear()
    app = create_app()
    authenticate_app(app)
    try:
        with TestClient(app) as c:
            resp = c.post(
                "/integration/github/pull-requests",
                json={"repo": "org/api", "title": "t", "head": "f", "base": "main"},
            )
        assert resp.status_code == 501, resp.text
    finally:
        integ._github_client_singleton.cache_clear()


def test_health_route_returns_healthresult(
    authenticate_app: Callable[..., FastAPI],
) -> None:
    app = create_app()
    authenticate_app(app)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"resources": {}})

    gh = GitHubClient(token="t", transport=httpx.MockTransport(handler))
    app.dependency_overrides[get_github_client_optional] = lambda: gh
    with TestClient(app) as c:
        resp = c.get("/integration/github/health")
    assert resp.status_code == 200, resp.text
    assert resp.json()["healthy"] is True


def test_health_route_not_configured(
    authenticate_app: Callable[..., FastAPI],
) -> None:
    app = create_app()
    authenticate_app(app)
    app.dependency_overrides[get_github_client_optional] = lambda: None
    with TestClient(app) as c:
        resp = c.get("/integration/github/health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["healthy"] is False
    assert body["status"] == "not_configured"


def test_audit_sink_redacts_detail() -> None:
    """The API audit sink scrubs any token-shaped substring before recording."""
    from forge_api.observability.audit import AuditCategory, AuditLog
    from forge_api.routers.integration import _github_audit_sink
    from forge_integrations import GitHubAuditEvent

    log = AuditLog()
    sink = _github_audit_sink(log)
    leaked = "ghs_abcdefghijklmnopqrstuvwxyz0123456789"
    sink(
        GitHubAuditEvent(
            action="open_pr",
            repo="org/api",
            status="error",
            status_code=500,
            latency_ms=12,
            payload_hash="deadbeef",
            detail=f"boom with token {leaked}",
        )
    )
    entries = log.query(category=AuditCategory.INTEGRATION)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.action == "github.open_pr"
    assert entry.target == "org/api"
    assert entry.payload_hash == "deadbeef"
    # The token-shaped substring is redacted out of the detail.
    assert leaked not in (entry.detail or "")
    assert "[REDACTED]" in (entry.detail or "")
