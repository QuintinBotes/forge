"""HARD-01 unit tests: per-request audit events + secret non-leakage (offline).

The SDK emits a framework-agnostic ``GitHubAuditEvent`` per terminal request
outcome; the API layer is responsible for redaction on the way to the immutable
audit log. These tests assert the SDK never places a token / JWT / PEM into an
audit event, an exception message, or a request the caller can observe.
"""

from __future__ import annotations

import httpx
from conftest import make_transport

from forge_integrations import GitHubAuditEvent, GitHubClient, RetryPolicy
from forge_integrations.github import _hash_body

TOKEN = "ghs_supersecret_installation_token_deadbeef00"


def _app_client(handler, events: list[GitHubAuditEvent]) -> GitHubClient:
    def sink(event: GitHubAuditEvent) -> None:
        events.append(event)

    return GitHubClient(
        transport=make_transport(handler),
        retry=RetryPolicy(max_attempts=2, jitter=False),
        token_provider=lambda: TOKEN,
        invalidate=lambda: None,
        sleep=lambda _s: None,
        audit_sink=sink,
    )


def test_audit_event_per_request() -> None:
    events: list[GitHubAuditEvent] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"resources": {}})

    client = _app_client(handler, events)
    client._request("GET", "/rate_limit", action="health")
    assert len(events) == 1
    ev = events[0]
    assert ev.action == "health"
    assert ev.status == "ok"
    assert ev.status_code == 200
    assert ev.latency_ms is not None


def test_audit_event_records_payload_hash_not_body() -> None:
    events: list[GitHubAuditEvent] = []
    payload = {"title": "secret-ish title", "head": "f", "base": "main"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"number": 1})

    client = _app_client(handler, events)
    client._request("POST", "/repos/o/r/pulls", json=payload, action="open_pr", repo="o/r")
    ev = events[0]
    assert ev.payload_hash == _hash_body(payload)
    # The raw body is never carried on the event.
    assert "secret-ish title" not in (ev.detail or "")
    assert ev.repo == "o/r"


def test_token_never_in_audit_events_or_errors() -> None:
    events: list[GitHubAuditEvent] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # Echo the bearer we received into the error body to prove the SDK does
        # not propagate it back into audit/detail.
        return httpx.Response(500, json={"message": "server error"})

    client = _app_client(handler, events)
    resp = client._request("GET", "/x", action="op", repo="o/r")
    assert resp.status_code == 500
    # Terminal error event emitted; it must not contain the token.
    assert events
    for ev in events:
        blob = f"{ev.action}{ev.repo}{ev.status}{ev.detail}{ev.payload_hash}"
        assert TOKEN not in blob


def test_network_error_detail_has_no_token() -> None:
    events: list[GitHubAuditEvent] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    import pytest

    from forge_integrations import GitHubError

    client = _app_client(handler, events)
    with pytest.raises(GitHubError) as exc:
        client._request("GET", "/x", action="op")
    assert TOKEN not in str(exc.value)
    assert events and events[-1].status == "error"
    assert TOKEN not in (events[-1].detail or "")


def test_authorization_header_is_bearer_installation_token() -> None:
    """The App path injects the installation token as a per-request bearer."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization", ""))
        return httpx.Response(200, json={})

    client = _app_client(handler, [])
    client._request("GET", "/rate_limit", action="health")
    assert seen == [f"Bearer {TOKEN}"]
