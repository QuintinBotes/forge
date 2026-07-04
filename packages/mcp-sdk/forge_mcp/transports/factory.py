"""``live_transport_factory`` — build a real transport per connection (HARD-05).

The manager (``MCPConnectionManager``) already accepts an injectable
``TransportFactory``; ALPHA wired only the ``NullTransport`` factory. This module
supplies the *live* factory: given an :class:`~forge_contracts.MCPConnection` it
returns the matching real transport, resolving the connection's token through an
injected ``token_resolver`` (the vault) **at construction time** — never held in
a module global, never logged.

RFC 8707 token-binding is enforced as a **precondition, before any I/O**: an
authenticated ``http``/``sse`` connection that resolves to no ``resource``
indicator (neither ``auth.resource`` nor ``endpoint``) raises
:class:`~forge_mcp.exceptions.MCPSecurityError` at factory time, so a bearer
token can never be sent without an audience (HARD-05 AC3).
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Callable, Sequence

from forge_contracts import MCPAuthType, MCPConnection, MCPTransport
from forge_mcp.exceptions import MCPSecurityError
from forge_mcp.manager import TransportFactory
from forge_mcp.security import token_binding
from forge_mcp.transport import Transport
from forge_mcp.transports.http import DEFAULT_PROTOCOL_VERSION, HttpMcpTransport
from forge_mcp.transports.stdio import StdioMcpTransport

#: Resolves a connection's bearer token (from the vault); ``None`` for auth.none.
TokenResolver = Callable[[MCPConnection], str | None]

#: Resolves the argv for a stdio connection (default: ``MCP_STDIO_COMMAND`` env).
StdioCommandResolver = Callable[[MCPConnection], Sequence[str]]


def _default_stdio_command(conn: MCPConnection) -> Sequence[str]:
    """Resolve a stdio server command from ``MCP_STDIO_COMMAND`` (shell-split)."""
    raw = os.environ.get("MCP_STDIO_COMMAND")
    if not raw:
        raise MCPSecurityError(
            f"stdio connection {conn.id!r} has no command "
            "(set MCP_STDIO_COMMAND or inject a stdio_command_resolver)"
        )
    return shlex.split(raw)


def live_transport_factory(
    *,
    token_resolver: TokenResolver,
    timeout_s: float = 30.0,
    protocol_version: str = DEFAULT_PROTOCOL_VERSION,
    stdio_command_resolver: StdioCommandResolver | None = None,
) -> TransportFactory:
    """Return a :class:`~forge_mcp.manager.TransportFactory` for live servers.

    * ``http`` / ``sse`` -> :class:`HttpMcpTransport` bound to ``conn.endpoint``,
      carrying the resolved token and the RFC 8707 ``resource`` indicator;
    * ``stdio`` -> :class:`StdioMcpTransport` over the resolved command.

    Raises :class:`MCPSecurityError` (before any I/O) for an authenticated
    http/sse connection with no resource binding, or a missing endpoint/command.
    """
    resolve_command = stdio_command_resolver or _default_stdio_command

    def _factory(conn: MCPConnection) -> Transport:
        transport = conn.transport
        if transport in (MCPTransport.HTTP, MCPTransport.SSE):
            authed = conn.auth.type is not MCPAuthType.NONE
            resource = token_binding(conn)  # auth.resource or endpoint; None for auth.none
            if authed and not resource:
                raise MCPSecurityError(
                    f"authenticated {transport.value} connection {conn.id!r} has no "
                    "RFC 8707 resource binding (set auth.resource or endpoint)"
                )
            if not conn.endpoint:
                raise MCPSecurityError(
                    f"{transport.value} connection {conn.id!r} requires an endpoint"
                )
            token = token_resolver(conn) if authed else None
            return HttpMcpTransport(
                conn.endpoint,
                token=token,
                resource=resource,
                protocol_version=protocol_version,
                timeout_s=timeout_s,
                sse_compat=transport is MCPTransport.SSE,
            )
        if transport is MCPTransport.STDIO:
            return StdioMcpTransport(
                resolve_command(conn),
                timeout_s=timeout_s,
                protocol_version=protocol_version,
            )
        raise MCPSecurityError(  # pragma: no cover - enum is exhaustive
            f"unsupported MCP transport {transport!r} for connection {conn.id!r}"
        )

    return _factory


__all__ = ["StdioCommandResolver", "TokenResolver", "live_transport_factory"]
