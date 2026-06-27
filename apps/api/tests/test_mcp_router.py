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
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from forge_api.main import create_app
from forge_api.routers.mcp import get_mcp_manager
from forge_mcp import MCPConnectionManager
from forge_mcp.testing import sample_connection, sample_transport


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app()
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
