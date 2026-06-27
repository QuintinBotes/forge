"""MCP connection manager — the shared control plane (plan Task 1.12).

Registers :class:`~forge_contracts.MCPConnection` records, builds a per-connection
:class:`~forge_mcp.client.MCPGatewayClient` (each with its own transport), and
shares a single append-only audit log across all connections so the gateway and
the API control plane can serve a unified, redacted audit trail.

The transport is created via an injectable factory. The default factory returns
:class:`~forge_mcp.transport.NullTransport`, which refuses live traffic — so no
real MCP network call ever happens unless a real transport is wired in
explicitly. Both the gateway service (``apps/mcp-gateway``) and the API
``/mcp/*`` router consume this manager.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from forge_contracts import (
    MCPAuditEntry,
    MCPConnection,
    MCPResource,
    MCPResourceContent,
    MCPToolResult,
)
from forge_mcp.audit import AuditSink, InMemoryAuditLog
from forge_mcp.client import MCPGatewayClient
from forge_mcp.exceptions import MCPConnectionNotFoundError
from forge_mcp.transport import NullTransport, Transport

if TYPE_CHECKING:
    from forge_contracts import Policy, PolicyEvaluator

#: A factory that produces a transport for a given connection.
TransportFactory = Callable[[MCPConnection], Transport]


def _null_transport_factory(conn: MCPConnection) -> Transport:
    """Default factory: a transport that refuses live MCP traffic."""
    return NullTransport()


class MCPConnectionManager:
    """Registry of MCP connections + their audited, scoped clients."""

    def __init__(
        self,
        *,
        transport_factory: TransportFactory | None = None,
        audit_log: AuditSink | None = None,
        policy: Policy | None = None,
        evaluator: PolicyEvaluator | None = None,
    ) -> None:
        self._transport_factory = transport_factory or _null_transport_factory
        # Explicit None check: an empty InMemoryAuditLog is falsy.
        self._audit: AuditSink = audit_log if audit_log is not None else InMemoryAuditLog()
        self._policy = policy
        self._evaluator = evaluator
        self._clients: dict[str, MCPGatewayClient] = {}
        self._connections: dict[str, MCPConnection] = {}

    # ------------------------------------------------------------------ #
    # Registration                                                       #
    # ------------------------------------------------------------------ #

    def register(self, conn: MCPConnection) -> MCPConnection:
        """Register ``conn`` and prepare its client (read-only by default)."""
        client = MCPGatewayClient(
            transport=self._transport_factory(conn),
            audit_log=self._audit,
            policy=self._policy,
            evaluator=self._evaluator,
        )
        client.connect(conn)
        self._clients[conn.id] = client
        self._connections[conn.id] = conn
        return conn

    def list_connections(self) -> list[MCPConnection]:
        return list(self._connections.values())

    def get(self, connection_id: str) -> MCPGatewayClient:
        try:
            return self._clients[connection_id]
        except KeyError as exc:
            raise MCPConnectionNotFoundError(
                f"no MCP connection registered with id {connection_id!r}"
            ) from exc

    # ------------------------------------------------------------------ #
    # Delegated operations                                               #
    # ------------------------------------------------------------------ #

    def list_resources(
        self, connection_id: str, namespace: str | None = None
    ) -> list[MCPResource]:
        return self.get(connection_id).list_resources(namespace)

    def read_resource(self, connection_id: str, uri: str) -> MCPResourceContent:
        return self.get(connection_id).read_resource(uri)

    def call_tool(
        self, connection_id: str, name: str, arguments: dict[str, object]
    ) -> MCPToolResult:
        return self.get(connection_id).call_tool(name, arguments)

    def audit_entries(self, connection_id: str | None = None) -> list[MCPAuditEntry]:
        if isinstance(self._audit, InMemoryAuditLog):
            if connection_id is None:
                return self._audit.entries
            return self._audit.for_connection(connection_id)
        return []


__all__ = ["MCPConnectionManager", "TransportFactory"]
