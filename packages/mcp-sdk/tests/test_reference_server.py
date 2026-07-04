"""Unit tests for the reference MCP server core + the no-socket guard (AC9).

The reference server's ``handle`` dispatch is pure and testable without any
transport. The no-socket test proves the default (``NullTransport``) path opens
no socket — the hermetic lane never touches the network.
"""

from __future__ import annotations

import socket

import pytest

from forge_mcp import MCPConnectionManager, MCPTransportUnavailableError
from forge_mcp.reference_server import PLANTED_SECRET, ReferenceMCPServer
from forge_mcp.testing import sample_connection


def test_initialize_captures_resource_indicator() -> None:
    core = ReferenceMCPServer()
    resp = core.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "_meta": {"resource": "https://canon/x"}},
        },
        headers={"Authorization": "Bearer whatever"},
    )
    assert resp["result"]["serverInfo"]["name"] == "forge-reference-mcp"
    assert core.inspect_dict() == {
        "resource_indicator": "https://canon/x",
        "had_authorization": True,
        "session_id": core.session_id,
        "protocol_version": "2025-06-18",
    }


def test_notification_returns_no_response() -> None:
    core = ReferenceMCPServer()
    assert core.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_resources_read_carries_planted_secret() -> None:
    core = ReferenceMCPServer()
    resp = core.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "resources/read",
            "params": {"uri": "confluence://engineering/page-1"},
        }
    )
    assert PLANTED_SECRET in resp["result"]["contents"][0]["text"]


def test_unknown_resource_is_jsonrpc_error() -> None:
    core = ReferenceMCPServer()
    resp = core.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "resources/read",
            "params": {"uri": "confluence://nope/x"},
        }
    )
    assert resp["error"]["code"] == -32602


def test_tools_list_carries_annotations() -> None:
    core = ReferenceMCPServer()
    resp = core.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
    tools = {t["name"]: t for t in resp["result"]["tools"]}
    assert tools["search_pages"]["annotations"]["readOnlyHint"] is True
    assert tools["create_page"]["annotations"]["destructiveHint"] is True


def test_unknown_method_is_method_not_found() -> None:
    core = ReferenceMCPServer()
    resp = core.handle({"jsonrpc": "2.0", "id": 5, "method": "does/not/exist"})
    assert resp["error"]["code"] == -32601


# --------------------------------------------------------------------------- #
# AC9 — the default (Null) transport path opens no socket                      #
# --------------------------------------------------------------------------- #


def test_null_transport_path_opens_no_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the default factory, a live op raises without connecting a socket."""

    def _boom(self: socket.socket, *args: object, **kwargs: object) -> None:
        raise AssertionError("the hermetic path must not open a socket")

    monkeypatch.setattr(socket.socket, "connect", _boom)
    monkeypatch.setattr(socket.socket, "connect_ex", _boom)

    mgr = MCPConnectionManager()  # default NullTransport factory
    mgr.register(sample_connection())
    with pytest.raises(MCPTransportUnavailableError):
        mgr.list_resources("confluence-engineering")
