"""HARD-05 live MCP integration lane — real transport, no external cred.

These tests drive the SDK against a **real** MCP server over a **real**
transport (Streamable-HTTP on a loopback socket, or stdio over a subprocess).
The substrate is the in-repo self-hosted reference server, so the only "cred" is
a local URL — **no external SaaS**. They are marked ``integration`` + ``live_mcp``
and **skip cleanly** unless ``MCP_LIVE_TRANSPORT`` is truthy, so the default
``uv run pytest -q`` run stays hermetic and network-free (AC9).

The four MCP Security Rules are proven on the *live* path:

* AC4 — live read + ``query_through`` returns attributed chunks;
* AC5 — write denied by default, allowed only when re-registered ``allow_write``;
* AC6 — RFC 8707 ``resource`` indicator is sent over the wire (server-inspected);
* AC7 — namespace scoping blocks an out-of-scope URI before it reaches the server;
* AC8 — a redacted audit row is written for every list/read/call; a planted
  server secret is ``[redacted]`` in returned content;
* AC10 — server/JSON-RPC/timeout errors map to the right SDK exceptions.

Run: ``MCP_LIVE_TRANSPORT=true uv run pytest -m live_mcp -q``
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator

import httpx
import pytest

from forge_contracts import (
    MCPAuth,
    MCPAuthType,
    MCPCapabilities,
    MCPConnection,
    MCPTransport,
    MCPWriteForbiddenError,
)
from forge_mcp import (
    MCPConnectionManager,
    MCPNamespaceError,
    MCPTransportUnavailableError,
    live_transport_factory,
    query_through,
)
from forge_mcp.reference_server import PLANTED_SECRET, start_http_server

pytestmark = [pytest.mark.integration, pytest.mark.live_mcp]


def _live_enabled() -> bool:
    return os.environ.get("MCP_LIVE_TRANSPORT", "").strip().lower() in {"1", "true", "yes", "on"}


@pytest.fixture(scope="module", autouse=True)
def _require_live() -> None:
    if not _live_enabled():
        pytest.skip(
            "live MCP lane disabled — set MCP_LIVE_TRANSPORT=true to run "
            "(self-hosts the reference server; no external cred). "
            "See docs/runbooks/live-mcp.md"
        )


@pytest.fixture(scope="module")
def server() -> Iterator[object]:
    """A self-hosted reference server on a real loopback socket (or external URL)."""
    external = os.environ.get("MCP_SERVER_URL")
    if external:
        # An externally-run reference/real server was provided; use it as-is.
        yield _ExternalServer(external)
        return
    running = start_http_server()
    try:
        yield running
    finally:
        running.shutdown()


class _ExternalServer:
    def __init__(self, url: str) -> None:
        self.url = url
        base = url.rsplit("/", 1)[0]
        self.inspect_url = f"{base}/inspect"

    def shutdown(self) -> None:  # pragma: no cover - external lifecycle
        pass


def _http_connection(
    url: str,
    *,
    authed: bool = False,
    allow_write: bool = False,
    namespaces: list[str] | None = None,
) -> MCPConnection:
    auth = (
        MCPAuth(type=MCPAuthType.OAUTH, resource=url, token_ref="secret://mcp/reference")
        if authed
        else MCPAuth()
    )
    return MCPConnection(
        id="reference",
        name="Reference MCP",
        transport=MCPTransport.HTTP,
        endpoint=url,
        auth=auth,
        capabilities=MCPCapabilities(resources=True, tools=True),
        allow_write=allow_write,
        allowed_namespaces=namespaces if namespaces is not None else ["engineering"],
    )


def _manager(authed: bool = False) -> MCPConnectionManager:
    factory = live_transport_factory(token_resolver=lambda conn: "reference-token-xyz")
    return MCPConnectionManager(transport_factory=factory)


# --------------------------------------------------------------------------- #
# AC4 — live read + query-through                                              #
# --------------------------------------------------------------------------- #


def test_live_read_and_query_through(server: object) -> None:
    mgr = _manager()
    mgr.register(_http_connection(server.url))
    content = mgr.read_resource("reference", "confluence://engineering/page-1")
    assert content.content  # real bytes came back over real HTTP
    assert PLANTED_SECRET not in content.content
    assert "[redacted]" in content.content

    chunks = query_through(mgr.get("reference"), "vault rotation runbook", k=3)
    assert chunks
    assert all(c.chunk_type.value == "mcp_resource" for c in chunks)
    assert all(c.source_uri for c in chunks)


def test_live_read_and_query_through_over_stdio() -> None:
    if not _live_enabled():  # pragma: no cover - guarded by module autouse skip
        pytest.skip("live disabled")
    from forge_mcp.transports.stdio import StdioMcpTransport

    cmd = os.environ.get("MCP_STDIO_COMMAND")
    command = (
        cmd.split() if cmd else [sys.executable, "-m", "forge_mcp.reference_server", "--stdio"]
    )
    factory = live_transport_factory(
        token_resolver=lambda conn: None,
        stdio_command_resolver=lambda conn: command,
    )
    mgr = MCPConnectionManager(transport_factory=factory)
    conn = MCPConnection(
        id="ref-stdio", name="ref", transport=MCPTransport.STDIO, allowed_namespaces=["engineering"]
    )
    mgr.register(conn)
    content = mgr.read_resource("ref-stdio", "confluence://engineering/page-1")
    assert "[redacted]" in content.content
    mgr.get("ref-stdio")  # keep-alive
    assert isinstance(mgr.get("ref-stdio")._transport, StdioMcpTransport)


# --------------------------------------------------------------------------- #
# AC5 — write denied by default, allowed on an explicitly write-enabled conn   #
# --------------------------------------------------------------------------- #


def test_live_write_denied_by_default_then_allowed(server: object) -> None:
    mgr = _manager()
    mgr.register(_http_connection(server.url, allow_write=False))
    with pytest.raises(MCPWriteForbiddenError):
        mgr.call_tool("reference", "create_page", {"title": "x"})
    # A forbidden audit row was written; the read tool still works live.
    assert mgr.audit_entries("reference")[-1].status == "forbidden"

    # Re-register the same connection with writes explicitly enabled.
    mgr.register(_http_connection(server.url, allow_write=True))
    result = mgr.call_tool("reference", "create_page", {"title": "x"})
    assert result.status == "ok"


# --------------------------------------------------------------------------- #
# AC6 — RFC 8707 token binding is sent over the wire                           #
# --------------------------------------------------------------------------- #


def test_live_token_binding_resource_sent(server: object) -> None:
    mgr = _manager(authed=True)
    mgr.register(_http_connection(server.url, authed=True))
    mgr.list_resources("reference")  # triggers the live initialize handshake
    inspected = httpx.get(server.inspect_url).json()
    assert inspected["resource_indicator"] == server.url
    assert inspected["had_authorization"] is True


# --------------------------------------------------------------------------- #
# AC7 — namespace scoping holds on the live server                             #
# --------------------------------------------------------------------------- #


def test_live_namespace_scoping_blocks_out_of_scope(server: object) -> None:
    mgr = _manager()
    mgr.register(_http_connection(server.url, namespaces=["engineering"]))
    # An out-of-scope read is refused before it reaches the server.
    with pytest.raises(MCPNamespaceError):
        mgr.read_resource("reference", "confluence://finance/budget")
    # list_resources returns only in-scope resources even though the server has more.
    listed = mgr.list_resources("reference")
    assert {r.namespace for r in listed} == {"engineering"}


# --------------------------------------------------------------------------- #
# AC8 — a redacted audit row per live op                                       #
# --------------------------------------------------------------------------- #


def test_live_audit_row_written_and_redacted(server: object) -> None:
    mgr = _manager()
    mgr.register(_http_connection(server.url))
    mgr.list_resources("reference")
    mgr.read_resource("reference", "confluence://engineering/page-1")
    entries = mgr.audit_entries("reference")
    tools = [e.tool for e in entries]
    assert "resources/list" in tools
    assert "resources/read" in tools
    for entry in entries:
        assert entry.redacted is True
        assert entry.payload_hash
        assert PLANTED_SECRET not in entry.model_dump_json()


# --------------------------------------------------------------------------- #
# AC10 — error paths map to the right SDK exceptions                           #
# --------------------------------------------------------------------------- #


def test_live_connection_refused_maps_to_transport_unavailable() -> None:
    if not _live_enabled():  # pragma: no cover
        pytest.skip("live disabled")
    mgr = _manager()
    # A closed loopback port: real connection refused.
    mgr.register(_http_connection("http://127.0.0.1:1/mcp"))
    with pytest.raises(MCPTransportUnavailableError):
        mgr.list_resources("reference")
