"""MCP connection manager — the shared control plane (plan Task 1.12).

Registers :class:`~scf_contracts.MCPConnection` records, builds a per-connection
:class:`~scf_mcp.client.MCPGatewayClient` (each with its own transport), and
shares a single append-only audit log across all connections so the gateway and
the API control plane can serve a unified, redacted audit trail.

The transport is created via an injectable factory. The default factory returns
:class:`~scf_mcp.transport.NullTransport`, which refuses live traffic — so no
real MCP network call ever happens unless a real transport is wired in
explicitly. Both the gateway service (``apps/mcp-gateway``) and the API
``/mcp/*`` router consume this manager.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING

from scf_contracts import (
    MCPAuditEntry,
    MCPConnection,
    MCPResource,
    MCPResourceContent,
    MCPToolResult,
)
from scf_mcp.audit import AuditSink, InMemoryAuditLog
from scf_mcp.client import MCPGatewayClient
from scf_mcp.exceptions import MCPConnectionNotFoundError
from scf_mcp.transport import NullTransport, PromptMessage, PromptSpec, Transport

if TYPE_CHECKING:
    from scf_contracts import Policy, PolicyEvaluator
    from scf_mcp.ratelimit import RateLimiter

#: A factory that produces a transport for a given connection.
TransportFactory = Callable[[MCPConnection], Transport]


#: A connection's registry key: the workspace that registered it, and its id.
_Key = tuple[uuid.UUID | None, str]


def _null_transport_factory(conn: MCPConnection) -> Transport:
    """Default factory: a transport that refuses live MCP traffic."""
    return NullTransport()


class _Tee:
    """Records each entry to the shared audit trail and to one connection's own."""

    def __init__(self, shared: AuditSink, own: InMemoryAuditLog) -> None:
        self._sinks = (shared, own)

    def record(self, entry: MCPAuditEntry) -> None:
        for sink in self._sinks:
            sink.record(entry)


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
        # Keyed by (workspace, connection id): a connection's id is a name its workspace
        # chooses, so two workspaces may both have one called "github" (PT-13). The
        # frozen ``MCPConnection`` contract carries no ``workspace_id``; ``None`` is a
        # connection registered without a tenant (the single-tenant ``apps/mcp-gateway``).
        self._clients: dict[_Key, MCPGatewayClient] = {}
        self._connections: dict[_Key, MCPConnection] = {}
        # Each connection's own trail, beside the shared one, so a same-named connection
        # in another workspace never reads it.
        self._audits: dict[_Key, InMemoryAuditLog] = {}

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
        key = (workspace_id, conn.id)
        own = self._audits.setdefault(key, InMemoryAuditLog())
        client = MCPGatewayClient(
            transport=self._transport_factory(conn),
            audit_log=_Tee(self._audit, own),
            policy=self._policy,
            evaluator=self._evaluator,
            rate_limiter=_scoped_limiter(self._rate_limiter, workspace_id),
        )
        client.connect(conn)
        self._clients[key] = client
        self._connections[key] = conn
        return conn

    def list_connections(self, *, workspace_id: uuid.UUID | None = None) -> list[MCPConnection]:
        """List registered connections, scoped to ``workspace_id`` when given."""
        return [
            conn
            for (owner, _), conn in self._connections.items()
            if workspace_id is None or owner == workspace_id
        ]

    def get(self, connection_id: str, *, workspace_id: uuid.UUID | None = None) -> MCPGatewayClient:
        """Resolve the client of the connection ``workspace_id`` registered as ``connection_id``.

        A connection that is missing *or* registered by another workspace raises
        :class:`MCPConnectionNotFoundError` (mapped to 404) so the control plane
        never leaks the existence of another tenant's MCP server.
        """
        try:
            return self._clients[workspace_id, connection_id]
        except KeyError as exc:
            raise MCPConnectionNotFoundError(
                f"no MCP connection registered with id {connection_id!r}"
            ) from exc

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
        if connection_id is not None:
            self.get(connection_id, workspace_id=workspace_id)  # 404 unless the caller's own
            return self._audits[workspace_id, connection_id].entries
        if isinstance(self._audit, InMemoryAuditLog):
            return self._audit.entries
        return []


__all__ = ["MCPConnectionManager", "TransportFactory"]


class _WorkspaceRateLimiter:
    """One workspace's buckets in a shared limiter: its keys are prefixed with it, so a
    same-named connection of another workspace never draws on this one's budget."""

    def __init__(self, limiter: RateLimiter, workspace_id: uuid.UUID) -> None:
        self._limiter = limiter
        self._prefix = f"{workspace_id}:"

    def allow(self, key: str) -> bool:
        return self._limiter.allow(self._prefix + key)

    def retry_after_s(self) -> float:
        return self._limiter.retry_after_s()


def _scoped_limiter(
    limiter: RateLimiter | None, workspace_id: uuid.UUID | None
) -> RateLimiter | None:
    if limiter is None or workspace_id is None:
        return limiter
    return _WorkspaceRateLimiter(limiter, workspace_id)
