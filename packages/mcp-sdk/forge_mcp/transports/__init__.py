"""Live MCP transports (HARD-05).

The single load-bearing seam :class:`forge_mcp.transport.Transport` had exactly
two implementations in ALPHA — ``FakeTransport`` (tests) and ``NullTransport``
(refuses live traffic). This package adds the **real** ones that speak the MCP
wire protocol to a live server:

* :class:`HttpMcpTransport` — MCP Streamable-HTTP (JSON-RPC 2.0 over HTTP);
* :class:`StdioMcpTransport` — JSON-RPC 2.0 over a subprocess' stdin/stdout;
* :func:`live_transport_factory` — the credential-aware ``TransportFactory`` the
  gateway/API wire in behind ``MCP_LIVE_TRANSPORT``.

The security logic (read-only default, RFC 8707 binding, namespace scoping,
redacted audit) lives in the client/security/factory layers; these transports
are pure wire adapters behind the stable ``Transport`` protocol.
"""

from __future__ import annotations

from forge_mcp.transports.factory import (
    StdioCommandResolver,
    TokenResolver,
    live_transport_factory,
)
from forge_mcp.transports.http import (
    DEFAULT_PROTOCOL_VERSION,
    MAX_RESPONSE_BYTES,
    HttpMcpTransport,
)
from forge_mcp.transports.jsonrpc import (
    JsonRpcError,
    build_notification,
    build_request,
    parse_response,
)
from forge_mcp.transports.stdio import StdioMcpTransport

__all__ = [
    "DEFAULT_PROTOCOL_VERSION",
    "MAX_RESPONSE_BYTES",
    "HttpMcpTransport",
    "JsonRpcError",
    "StdioCommandResolver",
    "StdioMcpTransport",
    "TokenResolver",
    "build_notification",
    "build_request",
    "live_transport_factory",
    "parse_response",
]
