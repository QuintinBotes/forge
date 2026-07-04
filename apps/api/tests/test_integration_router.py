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


def _post_webhook(
    client: TestClient, body: bytes, signature: str | None
) -> httpx.Response:
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
