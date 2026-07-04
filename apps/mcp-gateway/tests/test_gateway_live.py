"""HARD-05 gateway live lane — the /mcp/* HTTP surface over real transport.

Drives the gateway FastAPI service end-to-end against the self-hosted reference
MCP server over **real HTTP**: register a connection, list/read namespace-scoped
resources, get the audit trail, and confirm a write is denied by default. Marked
``integration`` + ``live_mcp`` and **skips cleanly** unless ``MCP_LIVE_TRANSPORT``
is truthy, so the default hermetic gateway suite is unchanged. No external cred.

Run: ``MCP_LIVE_TRANSPORT=true uv run pytest apps/mcp-gateway -m live_mcp -q``
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from forge_contracts import MCPConnection, MCPTransport
from forge_mcp.reference_server import PLANTED_SECRET, start_http_server
from forge_mcp_gateway.app import create_gateway_app

pytestmark = [pytest.mark.integration, pytest.mark.live_mcp]


def _live_enabled() -> bool:
    return os.environ.get("MCP_LIVE_TRANSPORT", "").strip().lower() in {"1", "true", "yes", "on"}


@pytest.fixture(autouse=True)
def _require_live(monkeypatch: pytest.MonkeyPatch) -> None:
    if not _live_enabled():
        pytest.skip(
            "live MCP lane disabled — set MCP_LIVE_TRANSPORT=true (see docs/runbooks/live-mcp.md)"
        )


@pytest.fixture
def server() -> Iterator[object]:
    running = start_http_server()
    try:
        yield running
    finally:
        running.shutdown()


@pytest.fixture
def client(server: object) -> TestClient:
    # create_gateway_app() reads MCP_LIVE_TRANSPORT and wires the live factory.
    app = create_gateway_app()
    tc = TestClient(app)
    conn = MCPConnection(
        id="reference",
        name="Reference MCP",
        transport=MCPTransport.HTTP,
        endpoint=server.url,
        allowed_namespaces=["engineering"],
    )
    resp = tc.post("/connections", json=conn.model_dump(mode="json"))
    assert resp.status_code == 201, resp.text
    return tc


def test_gateway_lists_scoped_resources_live(client: TestClient) -> None:
    resp = client.get("/connections/reference/resources")
    assert resp.status_code == 200
    namespaces = {r["namespace"] for r in resp.json()}
    assert namespaces == {"engineering"}


def test_gateway_reads_redacted_content_live(client: TestClient) -> None:
    resp = client.get(
        "/connections/reference/resources/read",
        params={"uri": "confluence://engineering/page-1"},
    )
    assert resp.status_code == 200
    content = resp.json()["content"]
    assert PLANTED_SECRET not in content
    assert "[redacted]" in content


def test_gateway_out_of_scope_read_is_403_live(client: TestClient) -> None:
    resp = client.get(
        "/connections/reference/resources/read",
        params={"uri": "confluence://finance/budget"},
    )
    assert resp.status_code == 403


def test_gateway_write_denied_by_default_live(client: TestClient) -> None:
    resp = client.post(
        "/connections/reference/tools/call",
        json={"name": "create_page", "arguments": {"title": "x"}},
    )
    assert resp.status_code == 403


def test_gateway_audit_trail_after_live_ops(client: TestClient) -> None:
    client.get("/connections/reference/resources")
    client.get(
        "/connections/reference/resources/read",
        params={"uri": "confluence://engineering/page-1"},
    )
    resp = client.get("/connections/reference/audit")
    assert resp.status_code == 200
    tools = {e["tool"] for e in resp.json()}
    assert {"resources/list", "resources/read"} <= tools
    assert PLANTED_SECRET not in resp.text
