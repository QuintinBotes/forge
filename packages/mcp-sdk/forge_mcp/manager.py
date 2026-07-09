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

import uuid
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
from forge_mcp.transport import NullTransport, PromptMessage, PromptSpec, Transport

if TYPE_CHECKING:
    from forge_contracts import Policy, PolicyEvaluator
    from forge_mcp.ratelimit import RateLimiter

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
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._transport_factory = transport_factory or _null_transport_factory
        # Explicit None check: an empty InMemoryAuditLog is falsy.
        self._audit: AuditSink = audit_log if audit_log is not None else InMemoryAuditLog()
        self._policy = policy
        self._evaluator = evaluator
        self._rate_limiter = rate_limiter
        self._clients: dict[str, MCPGatewayClient] = {}
        self._connections: dict[str, MCPConnection] = {}
        # Per-workspace ownership of each connection (Phase-2 bug fix r3). The
        # frozen ``MCPConnection`` contract carries no ``workspace_id`` field, so
        # tenancy is tracked alongside the registry. ``None`` means the connection
        # was registered without a tenant dimension (e.g. the single-tenant
        # ``apps/mcp-gateway`` service) and is not scoped.
        self._owners: dict[str, uuid.UUID] = {}

    # ------------------------------------------------------------------ #
    # Registration                                                       #
    # ------------------------------------------------------------------ #

    def register(
        self, conn: MCPConnection, *, workspace_id: uuid.UUID | None = None
    ) -> MCPConnection:
        """Register ``conn`` and prepare its client (read-only by default).

        When ``workspace_id`` is supplied the connection is bound to that tenant:
        all later reads/calls/audit must present the same workspace or be treated
        as if the connection does not exist (cross-tenant isolation).
        """
        client = MCPGatewayClient(
            transport=self._transport_factory(conn),
            audit_log=self._audit,
            policy=self._policy,
            evaluator=self._evaluator,
            rate_limiter=self._rate_limiter,
        )
        client.connect(conn)
        self._clients[conn.id] = client
        self._connections[conn.id] = conn
        if workspace_id is not None:
            self._owners[conn.id] = workspace_id
        return conn

    def list_connections(self, *, workspace_id: uuid.UUID | None = None) -> list[MCPConnection]:
        """List registered connections, scoped to ``workspace_id`` when given."""
        if workspace_id is None:
            return list(self._connections.values())
        return [
            conn for cid, conn in self._connections.items() if self._owners.get(cid) == workspace_id
        ]

    def get(self, connection_id: str, *, workspace_id: uuid.UUID | None = None) -> MCPGatewayClient:
        """Resolve a connection's client, enforcing tenant ownership.

        A connection that is missing *or* owned by a different workspace raises
        :class:`MCPConnectionNotFoundError` (mapped to 404) so the control plane
        never leaks the existence of another tenant's MCP server.
        """
        try:
            client = self._clients[connection_id]
        except KeyError as exc:
            raise MCPConnectionNotFoundError(
                f"no MCP connection registered with id {connection_id!r}"
            ) from exc
        if workspace_id is not None and self._owners.get(connection_id) != workspace_id:
            raise MCPConnectionNotFoundError(
                f"no MCP connection registered with id {connection_id!r}"
            )
        return client

    # ------------------------------------------------------------------ #
    # Delegated operations                                               #
    # ------------------------------------------------------------------ #

    def list_resources(
        self,
        connection_id: str,
        namespace: str | None = None,
        *,
        workspace_id: uuid.UUID | None = None,
    ) -> list[MCPResource]:
        return self.get(connection_id, workspace_id=workspace_id).list_resources(namespace)

    def read_resource(
        self, connection_id: str, uri: str, *, workspace_id: uuid.UUID | None = None
    ) -> MCPResourceContent:
        return self.get(connection_id, workspace_id=workspace_id).read_resource(uri)

    def call_tool(
        self,
        connection_id: str,
        name: str,
        arguments: dict[str, object],
        *,
        workspace_id: uuid.UUID | None = None,
    ) -> MCPToolResult:
        return self.get(connection_id, workspace_id=workspace_id).call_tool(name, arguments)

    def list_prompts(
        self, connection_id: str, *, workspace_id: uuid.UUID | None = None
    ) -> list[PromptSpec]:
        return self.get(connection_id, workspace_id=workspace_id).list_prompts()

    def get_prompt(
        self,
        connection_id: str,
        name: str,
        arguments: dict[str, object] | None = None,
        *,
        workspace_id: uuid.UUID | None = None,
    ) -> list[PromptMessage]:
        return self.get(connection_id, workspace_id=workspace_id).get_prompt(name, arguments)

    def audit_entries(
        self, connection_id: str | None = None, *, workspace_id: uuid.UUID | None = None
    ) -> list[MCPAuditEntry]:
        # Enforce tenant ownership before returning a connection's audit trail.
        if connection_id is not None and workspace_id is not None:
            self.get(connection_id, workspace_id=workspace_id)
        if isinstance(self._audit, InMemoryAuditLog):
            if connection_id is None:
                return self._audit.entries
            return self._audit.for_connection(connection_id)
        return []


__all__ = ["MCPConnectionManager", "TransportFactory"]
