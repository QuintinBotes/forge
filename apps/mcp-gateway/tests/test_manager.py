"""Tests for the MCP connection manager (plan Task 1.12).

The manager is the gateway's control plane: it registers connections, builds a
per-connection :class:`~forge_mcp.MCPGatewayClient`, and aggregates a shared
audit log. Live transport is mocked.
"""

from __future__ import annotations

import pytest

from forge_contracts import MCPConnection, MCPToolResult, MCPWriteForbiddenError
from forge_mcp import MCPConnectionNotFoundError
from forge_mcp.testing import FakeTransport, sample_connection, sample_transport
from forge_mcp_gateway.manager import MCPConnectionManager


def _manager() -> MCPConnectionManager:
    return MCPConnectionManager(transport_factory=lambda conn: sample_transport())


def test_register_and_list_connections() -> None:
    mgr = _manager()
    conn = mgr.register(sample_connection())
    assert isinstance(conn, MCPConnection)
    assert [c.id for c in mgr.list_connections()] == ["confluence-engineering"]


def test_register_defaults_to_read_only() -> None:
    mgr = _manager()
    conn = mgr.register(sample_connection())
    assert conn.allow_write is False


def test_get_unknown_connection_raises() -> None:
    mgr = _manager()
    with pytest.raises(MCPConnectionNotFoundError):
        mgr.get("nope")


def test_list_resources_is_namespace_scoped() -> None:
    mgr = _manager()
    mgr.register(sample_connection())
    resources = mgr.list_resources("confluence-engineering")
    assert all(r.namespace in {"engineering", "architecture"} for r in resources)


def test_call_tool_ok_and_audited() -> None:
    mgr = _manager()
    mgr.register(sample_connection())
    result = mgr.call_tool("confluence-engineering", "search_pages", {"q": "vault"})
    assert isinstance(result, MCPToolResult)
    assert result.status == "ok"
    audit = mgr.audit_entries("confluence-engineering")
    assert audit and audit[-1].tool == "search_pages"


def test_write_tool_rejected_on_read_only_connection() -> None:
    mgr = _manager()
    mgr.register(sample_connection())
    with pytest.raises(MCPWriteForbiddenError):
        mgr.call_tool("confluence-engineering", "create_page", {"title": "x"})


def test_audit_log_is_shared_across_connections() -> None:
    transports = {
        "a": sample_transport(),
        "b": sample_transport(),
    }
    mgr = MCPConnectionManager(transport_factory=lambda conn: transports[conn.id])
    mgr.register(sample_connection(id="a"))
    mgr.register(sample_connection(id="b"))
    mgr.call_tool("a", "search_pages", {"q": "x"})
    mgr.call_tool("b", "get_document", {"q": "y"})
    assert len(mgr.audit_entries()) == 2
    assert len(mgr.audit_entries("a")) == 1


def test_default_transport_factory_blocks_live_calls() -> None:
    # No transport injected => live ops are unavailable (no real network).
    from forge_mcp import MCPTransportUnavailableError

    mgr = MCPConnectionManager()
    mgr.register(sample_connection())
    with pytest.raises(MCPTransportUnavailableError):
        mgr.list_resources("confluence-engineering")


def test_register_uses_fresh_transport_per_connection() -> None:
    made: list[FakeTransport] = []

    def factory(conn: MCPConnection) -> FakeTransport:
        tr = sample_transport()
        made.append(tr)
        return tr

    mgr = MCPConnectionManager(transport_factory=factory)
    mgr.register(sample_connection(id="a"))
    mgr.register(sample_connection(id="b"))
    assert len(made) == 2
