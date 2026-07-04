"""Unit tests for :func:`live_transport_factory` (HARD-05 AC3).

The factory selects the right transport per ``conn.transport`` and enforces the
RFC 8707 token-binding precondition *before any I/O* — an authenticated http/sse
connection with no ``resource`` binding is rejected at factory time.
"""

from __future__ import annotations

import pytest

from forge_contracts import (
    MCPAuth,
    MCPAuthType,
    MCPConnection,
    MCPTransport,
)
from forge_mcp.exceptions import MCPSecurityError
from forge_mcp.transports import live_transport_factory
from forge_mcp.transports.http import HttpMcpTransport
from forge_mcp.transports.stdio import StdioMcpTransport


def _factory(**kw):
    return live_transport_factory(token_resolver=lambda conn: "resolved-token", **kw)


def test_factory_builds_http_transport_for_http_connection() -> None:
    conn = MCPConnection(
        id="c", name="c", transport=MCPTransport.HTTP, endpoint="https://mcp.test/x"
    )
    tr = _factory()(conn)
    assert isinstance(tr, HttpMcpTransport)


def test_factory_builds_stdio_transport_for_stdio_connection() -> None:
    conn = MCPConnection(id="c", name="c", transport=MCPTransport.STDIO)
    tr = _factory(stdio_command_resolver=lambda conn: ["echo", "hi"])(conn)
    assert isinstance(tr, StdioMcpTransport)


def test_factory_maps_sse_to_http_transport() -> None:
    conn = MCPConnection(
        id="c", name="c", transport=MCPTransport.SSE, endpoint="https://mcp.test/x"
    )
    tr = _factory()(conn)
    assert isinstance(tr, HttpMcpTransport)


def test_factory_rejects_authed_http_without_resource_binding() -> None:
    # Authenticated, but neither auth.resource nor endpoint -> no RFC 8707 binding.
    conn = MCPConnection(
        id="c",
        name="c",
        transport=MCPTransport.HTTP,
        endpoint=None,
        auth=MCPAuth(type=MCPAuthType.OAUTH),
    )
    with pytest.raises(MCPSecurityError):
        _factory()(conn)


def test_factory_accepts_authed_http_with_endpoint_fallback_binding() -> None:
    conn = MCPConnection(
        id="c",
        name="c",
        transport=MCPTransport.HTTP,
        endpoint="https://mcp.test/x",
        auth=MCPAuth(type=MCPAuthType.OAUTH),
    )
    assert isinstance(_factory()(conn), HttpMcpTransport)


def test_factory_requires_endpoint_for_http() -> None:
    conn = MCPConnection(
        id="c",
        name="c",
        transport=MCPTransport.HTTP,
        endpoint=None,
        auth=MCPAuth(type=MCPAuthType.NONE),
    )
    with pytest.raises(MCPSecurityError):
        _factory()(conn)


def test_factory_token_resolved_only_for_authed_connections() -> None:
    calls: list[str] = []

    def resolver(conn: MCPConnection) -> str | None:
        calls.append(conn.id)
        return "tok"

    factory = live_transport_factory(token_resolver=resolver)
    # auth.none -> resolver is not consulted.
    factory(MCPConnection(id="unauth", name="c", endpoint="https://mcp.test/x"))
    assert calls == []
    # authed -> resolver is consulted at construction time.
    factory(
        MCPConnection(
            id="authed",
            name="c",
            endpoint="https://mcp.test/x",
            auth=MCPAuth(type=MCPAuthType.OAUTH),
        )
    )
    assert calls == ["authed"]


def test_factory_stdio_missing_command_raises() -> None:
    conn = MCPConnection(id="c", name="c", transport=MCPTransport.STDIO)
    with pytest.raises(MCPSecurityError):
        _factory()(conn)  # no MCP_STDIO_COMMAND, no resolver
