"""HARD-05 API live lane — /mcp/* over real transport + durable audit bridge.

Drives the API ``/mcp/*`` router against the self-hosted reference MCP server
over **real HTTP**, with the live transport factory and the platform audit
bridge wired in (``TeeAuditLog(MCPAuditSink(...))``). Proves that a live
list/read writes a redacted ``MCP_CALL`` row to the platform audit log, and that
a write is denied by default. Marked ``integration`` + ``live_mcp`` and skips
cleanly unless ``MCP_LIVE_TRANSPORT`` is truthy — no external cred.

The **durable-Postgres** row assertion (``FORGE_MCP_AUDIT_BACKEND=db`` against a
real ``AuditStore``) is parked to the integration lane with a live PG; this test
proves the bridge against the in-memory platform store.

Run: ``MCP_LIVE_TRANSPORT=true uv run pytest apps/api -m live_mcp -q``
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forge_api.main import create_app
from forge_api.observability import AuditCategory, AuditLog, MCPAuditSink
from forge_api.routers.mcp import get_mcp_manager
from forge_contracts import MCPConnection, MCPTransport
from forge_mcp import MCPConnectionManager, TeeAuditLog, live_transport_factory
from forge_mcp.reference_server import PLANTED_SECRET, start_http_server

pytestmark = [pytest.mark.integration, pytest.mark.live_mcp]


def _live_enabled() -> bool:
    return os.environ.get("MCP_LIVE_TRANSPORT", "").strip().lower() in {"1", "true", "yes", "on"}


@pytest.fixture(autouse=True)
def _require_live() -> None:
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
def platform_log() -> AuditLog:
    return AuditLog()


@pytest.fixture
def client(
    authenticate_app: Callable[..., FastAPI], server: object, platform_log: AuditLog
) -> Iterator[TestClient]:
    app = create_app()
    authenticate_app(app)
    factory = live_transport_factory(token_resolver=lambda conn: None)
    manager = MCPConnectionManager(
        transport_factory=factory,
        audit_log=TeeAuditLog(MCPAuditSink(platform_log)),
    )
    app.dependency_overrides[get_mcp_manager] = lambda: manager
    with TestClient(app) as c:
        conn = MCPConnection(
            id="reference",
            name="Reference MCP",
            transport=MCPTransport.HTTP,
            endpoint=server.url,
            allowed_namespaces=["engineering"],
        )
        assert c.post("/mcp/connections", json=conn.model_dump(mode="json")).status_code == 201
        yield c


def test_live_read_writes_redacted_platform_audit_row(
    client: TestClient, platform_log: AuditLog
) -> None:
    client.get("/mcp/connections/reference/resources")
    resp = client.get(
        "/mcp/connections/reference/resources/read",
        params={"uri": "confluence://engineering/page-1"},
    )
    assert resp.status_code == 200
    assert PLANTED_SECRET not in resp.text
    assert "[redacted]" in resp.json()["content"]

    rows = platform_log.query(category=AuditCategory.MCP_CALL)
    tools = {r.action for r in rows}
    assert {"resources/list", "resources/read"} <= tools
    for row in rows:
        assert row.redacted is True
        assert row.connection_id == "reference"
        assert PLANTED_SECRET not in row.model_dump_json()
    assert platform_log.verify_integrity() is True


def test_live_write_denied_by_default(client: TestClient) -> None:
    resp = client.post(
        "/mcp/connections/reference/tools/call",
        json={"name": "create_page", "arguments": {"title": "x"}},
    )
    assert resp.status_code == 403
