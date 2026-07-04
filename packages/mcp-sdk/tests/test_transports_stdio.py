"""Unit tests for :class:`StdioMcpTransport` (HARD-05 AC1/AC2).

Hermetic and network-free: the transport spawns the in-repo reference server as
a *local subprocess* and exchanges real newline-delimited JSON-RPC over its
stdin/stdout. No socket is opened.
"""

from __future__ import annotations

import sys

import pytest

from forge_contracts import MCPResourceContent
from forge_mcp.exceptions import MCPSecurityError, MCPTransportUnavailableError
from forge_mcp.reference_server import PLANTED_SECRET
from forge_mcp.transport import ToolSpec, Transport
from forge_mcp.transports.stdio import StdioMcpTransport

REFERENCE_CMD = [sys.executable, "-m", "forge_mcp.reference_server", "--stdio"]


@pytest.fixture
def transport() -> StdioMcpTransport:
    tr = StdioMcpTransport(REFERENCE_CMD)
    yield tr
    tr.close()


def test_stdio_transport_conforms_to_protocol(transport: StdioMcpTransport) -> None:
    assert isinstance(transport, Transport)


def test_empty_command_is_rejected() -> None:
    with pytest.raises(MCPSecurityError):
        StdioMcpTransport([])


def test_stdio_list_resources(transport: StdioMcpTransport) -> None:
    uris = [r.uri for r in transport.list_resources()]
    assert "confluence://engineering/page-1" in uris


def test_stdio_read_resource_returns_content(transport: StdioMcpTransport) -> None:
    content = transport.read_resource("confluence://engineering/page-1")
    assert isinstance(content, MCPResourceContent)
    # The raw server bytes carry the planted secret; redaction is the client's job.
    assert PLANTED_SECRET in content.content


def test_stdio_tool_hint_mapping(transport: StdioMcpTransport) -> None:
    tools = {t.name: t for t in transport.list_tools()}
    assert isinstance(tools["search_pages"], ToolSpec)
    assert tools["search_pages"].read_only is True
    assert tools["create_page"].destructive is True


def test_stdio_call_tool(transport: StdioMcpTransport) -> None:
    result = transport.call_tool("get_document", {"uri": "x"})
    assert result["isError"] is False


def test_stdio_spawn_failure_maps_to_transport_unavailable() -> None:
    tr = StdioMcpTransport(["/nonexistent/forge-mcp-server-xyz"])
    with pytest.raises(MCPTransportUnavailableError):
        tr.list_resources()
    tr.close()
