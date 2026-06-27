"""Tests for the MCP gateway FastAPI service (plan Task 1.12).

Exercises the HTTP control plane end-to-end against a fake transport: register a
connection, list/read namespace-scoped resources, call a read tool, get the
audit trail, and confirm a write tool on a read-only connection is rejected with
HTTP 403. No live MCP traffic.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from forge_mcp.testing import sample_connection, sample_transport
from forge_mcp_gateway.app import create_gateway_app
from forge_mcp_gateway.manager import MCPConnectionManager


@pytest.fixture
def client() -> TestClient:
    manager = MCPConnectionManager(transport_factory=lambda conn: sample_transport())
    app = create_gateway_app(manager=manager)
    return TestClient(app)


@pytest.fixture
def registered(client: TestClient) -> TestClient:
    resp = client.post("/connections", json=sample_connection().model_dump(mode="json"))
    assert resp.status_code == 201, resp.text
    return client


def test_health(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_register_connection_defaults_read_only(client: TestClient) -> None:
    body = sample_connection().model_dump(mode="json")
    body.pop("allow_write", None)
    resp = client.post("/connections", json=body)
    assert resp.status_code == 201
    assert resp.json()["allow_write"] is False


def test_list_connections(registered: TestClient) -> None:
    resp = registered.get("/connections")
    assert resp.status_code == 200
    assert [c["id"] for c in resp.json()] == ["confluence-engineering"]


def test_list_resources_namespace_scoped(registered: TestClient) -> None:
    resp = registered.get("/connections/confluence-engineering/resources")
    assert resp.status_code == 200
    namespaces = {r["namespace"] for r in resp.json()}
    assert "finance" not in namespaces


def test_list_resources_requested_namespace(registered: TestClient) -> None:
    resp = registered.get(
        "/connections/confluence-engineering/resources", params={"namespace": "engineering"}
    )
    assert resp.status_code == 200
    assert all(r["namespace"] == "engineering" for r in resp.json())


def test_read_resource(registered: TestClient) -> None:
    resp = registered.get(
        "/connections/confluence-engineering/resources/read",
        params={"uri": "confluence://engineering/page-1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["uri"] == "confluence://engineering/page-1"
    # Secret in the fixture content is redacted.
    assert "sk-fixture-secret-123" not in resp.text


def test_read_resource_out_of_scope_403(registered: TestClient) -> None:
    resp = registered.get(
        "/connections/confluence-engineering/resources/read",
        params={"uri": "confluence://finance/budget"},
    )
    assert resp.status_code == 403


def test_call_read_tool_ok(registered: TestClient) -> None:
    resp = registered.post(
        "/connections/confluence-engineering/tools/call",
        json={"name": "search_pages", "arguments": {"q": "vault"}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["payload_hash"]


def test_call_write_tool_forbidden(registered: TestClient) -> None:
    resp = registered.post(
        "/connections/confluence-engineering/tools/call",
        json={"name": "create_page", "arguments": {"title": "x"}},
    )
    assert resp.status_code == 403


def test_audit_endpoint_returns_redacted_entries(registered: TestClient) -> None:
    registered.post(
        "/connections/confluence-engineering/tools/call",
        json={"name": "search_pages", "arguments": {"q": "x", "token": "leak-me"}},
    )
    resp = registered.get("/connections/confluence-engineering/audit")
    assert resp.status_code == 200
    entries = resp.json()
    assert entries
    assert all(e["redacted"] is True for e in entries)
    assert "leak-me" not in resp.text


def test_unknown_connection_404(client: TestClient) -> None:
    resp = client.get("/connections/does-not-exist/resources")
    assert resp.status_code == 404
