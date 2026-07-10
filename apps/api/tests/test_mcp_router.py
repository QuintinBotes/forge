"""Integration tests for the MCP router (Task 1.12 fills ``/mcp/*``).

These exercise the real handlers wired to an :class:`MCPConnectionManager` whose
transport is the fixture-backed :class:`forge_mcp.testing.FakeTransport` (the live
transport is mocked — no network traffic). They prove the route layer:

* registers + lists connections (read-only by default),
* enforces the MCP security rules through HTTP status codes
  (write tool on a read-only connection -> 403; out-of-scope read -> 403;
  unknown connection -> 404),
* filters resources by namespace scope, and
* records a redacted audit entry per tool call.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forge_api.deps import Principal
from forge_api.main import create_app
from forge_api.routers.mcp import get_mcp_manager
from forge_contracts import UserRole
from forge_mcp import (
    InMemoryRateLimiter,
    MCPConnectionManager,
    MCPWriteApprovalEvaluator,
    RateLimiter,
    default_mcp_policy,
)
from forge_mcp.testing import FakeTransport, sample_connection, sample_transport


@pytest.fixture
def client(authenticate_app: Callable[..., FastAPI]) -> Iterator[TestClient]:
    app = create_app()
    authenticate_app(app)
    # A manager whose every connection gets a fresh fixture transport: no live
    # MCP traffic ever happens (plan Task 1.12: "live transport mocked").
    manager = MCPConnectionManager(transport_factory=lambda conn: sample_transport())
    app.dependency_overrides[get_mcp_manager] = lambda: manager
    with TestClient(app) as c:
        yield c


def _register(client: TestClient, **overrides: object) -> dict[str, object]:
    conn = sample_connection(**overrides)
    resp = client.post("/mcp/connections", json=conn.model_dump(mode="json"))
    assert resp.status_code == 201, resp.text
    return resp.json()


def _member_principal() -> Principal:
    return Principal(
        user_id=uuid.UUID("00000000-0000-0000-0000-0000000000b2"),
        workspace_id=uuid.UUID("00000000-0000-0000-0000-0000000000a1"),
        role=UserRole.MEMBER,
        email="member@forge.local",
        auth_method="test",
        scopes=["*"],
    )


# --------------------------------------------------------------------------- #
# Connections                                                                  #
# --------------------------------------------------------------------------- #


def test_list_connections_starts_empty(client: TestClient) -> None:
    resp = client.get("/mcp/connections")
    assert resp.status_code == 200
    assert resp.json() == []


def test_register_and_list_connection(client: TestClient) -> None:
    created = _register(client)
    assert created["id"] == "confluence-engineering"
    # Spec MCP rule 1: connections are read-only by default.
    assert created["allow_write"] is False

    listed = client.get("/mcp/connections")
    assert listed.status_code == 200
    ids = [c["id"] for c in listed.json()]
    assert ids == ["confluence-engineering"]


# --------------------------------------------------------------------------- #
# Resources (namespace scoping)                                                #
# --------------------------------------------------------------------------- #


def test_namespace_scoping_filters_resources(client: TestClient) -> None:
    _register(client)
    resp = client.get("/mcp/connections/confluence-engineering/resources")
    assert resp.status_code == 200
    namespaces = {r["namespace"] for r in resp.json()}
    # allowed_namespaces is {engineering, architecture}; finance is filtered out.
    assert namespaces == {"engineering", "architecture"}
    assert "finance" not in namespaces


def test_read_in_scope_resource_redacts_secret(client: TestClient) -> None:
    _register(client)
    resp = client.get(
        "/mcp/connections/confluence-engineering/resources/read",
        params={"uri": "confluence://engineering/page-1"},
    )
    assert resp.status_code == 200
    content = resp.json()["content"]
    # Rule 6: secrets in resource content are redacted before they leave the API.
    assert "sk-fixture-secret-123" not in content
    assert "[redacted]" in content


def test_read_out_of_scope_resource_is_403(client: TestClient) -> None:
    _register(client)
    resp = client.get(
        "/mcp/connections/confluence-engineering/resources/read",
        params={"uri": "confluence://finance/budget"},
    )
    assert resp.status_code == 403


def test_unknown_connection_is_404(client: TestClient) -> None:
    resp = client.get(f"/mcp/connections/{uuid.uuid4()}/resources")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Tools (write-gated, audited)                                                 #
# --------------------------------------------------------------------------- #


def test_read_only_connection_rejects_write_tool(client: TestClient) -> None:
    _register(client)
    resp = client.post(
        "/mcp/connections/confluence-engineering/tools/call",
        json={"name": "create_page", "arguments": {"title": "x"}},
    )
    # Rule 1: a write tool on a read-only connection is forbidden.
    assert resp.status_code == 403


def test_read_tool_call_succeeds(client: TestClient) -> None:
    _register(client)
    resp = client.post(
        "/mcp/connections/confluence-engineering/tools/call",
        json={"name": "search_pages", "arguments": {"q": "vault"}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["tool"] == "search_pages"
    assert body["status"] == "ok"
    assert body["payload_hash"]


def test_tool_call_records_redacted_audit_entry(client: TestClient) -> None:
    _register(client)
    client.post(
        "/mcp/connections/confluence-engineering/tools/call",
        json={"name": "search_pages", "arguments": {"token": "super-secret"}},
    )
    resp = client.get("/mcp/connections/confluence-engineering/audit")
    assert resp.status_code == 200
    entries = resp.json()
    assert len(entries) == 1
    entry = entries[0]
    assert entry["tool"] == "search_pages"
    assert entry["connection_id"] == "confluence-engineering"
    # Rule 4: audit records a payload hash, not the raw (secret-bearing) payload.
    assert entry["payload_hash"]
    assert entry["redacted"] is True
    assert "super-secret" not in str(entry)


def test_audit_unknown_connection_is_404(client: TestClient) -> None:
    resp = client.get(f"/mcp/connections/{uuid.uuid4()}/audit")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# F40 delta 1: write tool -> admin-only + routed through approval             #
# --------------------------------------------------------------------------- #


def _approving_client(
    authenticate_app: Callable[..., FastAPI],
    *,
    principal: Principal | None = None,
    transport_factory: Callable[[object], FakeTransport] | None = None,
    rate_limiter: RateLimiter | None = None,
) -> Iterator[TestClient]:
    app = create_app()
    authenticate_app(app, principal)
    manager = MCPConnectionManager(
        transport_factory=transport_factory or (lambda conn: sample_transport()),
        policy=default_mcp_policy(),
        evaluator=MCPWriteApprovalEvaluator(),
        rate_limiter=rate_limiter,
    )
    app.dependency_overrides[get_mcp_manager] = lambda: manager
    with TestClient(app) as c:
        yield c


@pytest.fixture
def approving_client(authenticate_app: Callable[..., FastAPI]) -> Iterator[TestClient]:
    yield from _approving_client(authenticate_app)


def test_write_tool_call_requires_approval_403(approving_client: TestClient) -> None:
    _register(approving_client, allow_write=True)
    resp = approving_client.post(
        "/mcp/connections/confluence-engineering/tools/call",
        json={"name": "create_page", "arguments": {"title": "x"}},
    )
    # allow_write clears rule 1; the approval gate then requires a human decision.
    assert resp.status_code == 403
    assert "approval" in resp.text.lower()


def test_write_tool_call_forbidden_for_non_admin(
    authenticate_app: Callable[..., FastAPI],
) -> None:
    gen = _approving_client(authenticate_app, principal=_member_principal())
    client = next(gen)
    try:
        _register(client, allow_write=True)
        resp = client.post(
            "/mcp/connections/confluence-engineering/tools/call",
            json={"name": "create_page", "arguments": {"title": "x"}},
        )
        # A RUN_AGENT member cannot invoke a write MCP tool (admin-only action).
        assert resp.status_code == 403
        assert "admin" in resp.text.lower()
    finally:
        gen.close()


def test_read_tool_call_still_succeeds_with_approval_wired(
    approving_client: TestClient,
) -> None:
    _register(approving_client)
    resp = approving_client.post(
        "/mcp/connections/confluence-engineering/tools/call",
        json={"name": "search_pages", "arguments": {"q": "vault"}},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# --------------------------------------------------------------------------- #
# F40 delta 2: prompts consumed                                               #
# --------------------------------------------------------------------------- #


def test_list_prompts(client: TestClient) -> None:
    _register(client)
    resp = client.get("/mcp/connections/confluence-engineering/prompts")
    assert resp.status_code == 200
    names = {p["name"] for p in resp.json()}
    assert "summarize_page" in names


def test_get_prompt_redacts_secret(client: TestClient) -> None:
    _register(client)
    resp = client.post(
        "/mcp/connections/confluence-engineering/prompts/get",
        json={"name": "summarize_page", "arguments": {"uri": "confluence://engineering/page-1"}},
    )
    assert resp.status_code == 200
    messages = resp.json()
    assert messages
    joined = " ".join(m["content"] for m in messages)
    assert "sk-fixture-secret-123" not in joined
    assert "[redacted]" in joined


# --------------------------------------------------------------------------- #
# F40 delta 3: elicitation surfaced (428)                                     #
# --------------------------------------------------------------------------- #


def test_elicitation_request_surfaced_as_428(
    authenticate_app: Callable[..., FastAPI],
) -> None:
    def factory(conn: object) -> FakeTransport:
        return FakeTransport(
            elicitations={
                "search_pages": {
                    "message": "Which space?",
                    "requestedSchema": {"type": "object"},
                }
            }
        )

    gen = _approving_client(authenticate_app, transport_factory=factory)
    client = next(gen)
    try:
        _register(client)
        resp = client.post(
            "/mcp/connections/confluence-engineering/tools/call",
            json={"name": "search_pages", "arguments": {"q": "x"}},
        )
        assert resp.status_code == 428
        detail = resp.json()["detail"]
        assert detail["error"] == "elicitation_required"
        assert detail["message"] == "Which space?"
    finally:
        gen.close()


# --------------------------------------------------------------------------- #
# F40 delta 4: rate limit -> 429 (typed, not a run failure)                   #
# --------------------------------------------------------------------------- #


def test_rate_limited_tool_call_returns_429(
    authenticate_app: Callable[..., FastAPI],
) -> None:
    limiter = InMemoryRateLimiter(capacity=1, refill_per_sec=0.001)
    gen = _approving_client(authenticate_app, rate_limiter=limiter)
    client = next(gen)
    try:
        _register(client)
        first = client.post(
            "/mcp/connections/confluence-engineering/tools/call",
            json={"name": "search_pages", "arguments": {"q": "1"}},
        )
        assert first.status_code == 200
        second = client.post(
            "/mcp/connections/confluence-engineering/tools/call",
            json={"name": "search_pages", "arguments": {"q": "2"}},
        )
        assert second.status_code == 429
        assert "retry-after" in {k.lower() for k in second.headers}
    finally:
        gen.close()
