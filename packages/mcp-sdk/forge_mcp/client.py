"""The MCP client: audited, read-only-by-default, namespace-scoped.

Implements the frozen :class:`forge_contracts.MCPClient` protocol. The client
wraps a :class:`~forge_mcp.transport.Transport` (the live wire — mocked in
tests) with the spec's MCP Security Rules:

1. read-only by default (``allow_write=False``); write tools are rejected,
2. tokens bound to the server via RFC 8707 ``resource`` on ``connect``,
3. inputs validated before any tool executes,
4. every call audited (tool, redacted payload hash, status, latency),
5. per-connection namespace scoping on resources,
6. secrets redacted from results and the audit log,
7. an optional policy evaluator gates calls like any other agent tool.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from forge_contracts import (
    MCPAuditEntry,
    MCPClient,
    MCPConnection,
    MCPResource,
    MCPResourceContent,
    MCPToolResult,
    MCPWriteForbiddenError,
    PolicyViolationError,
    ToolCall,
)
from forge_contracts.exceptions import ApprovalRequiredError
from forge_mcp.audit import AuditSink, InMemoryAuditLog, build_audit_entry
from forge_mcp.exceptions import (
    MCPInputError,
    MCPNamespaceError,
    MCPSecurityError,
)
from forge_mcp.security import (
    filter_resources,
    is_write_tool,
    namespace_of,
    payload_hash,
    redact,
    resource_in_scope,
    token_binding,
)
from forge_mcp.transport import NullTransport, ToolSpec, Transport

if TYPE_CHECKING:
    from forge_contracts import Policy, PolicyEvaluator


class MCPGatewayClient:
    """Concrete :class:`forge_contracts.MCPClient` over a single connection."""

    def __init__(
        self,
        transport: Transport | None = None,
        *,
        audit_log: AuditSink | None = None,
        policy: Policy | None = None,
        evaluator: PolicyEvaluator | None = None,
    ) -> None:
        # Explicit None checks: an empty InMemoryAuditLog is falsy (``__len__``
        # returns 0), so ``audit_log or ...`` would silently drop a shared log.
        self._transport: Transport = transport if transport is not None else NullTransport()
        self._audit: AuditSink = audit_log if audit_log is not None else InMemoryAuditLog()
        self._policy = policy
        self._evaluator = evaluator
        self._connection: MCPConnection | None = None
        self._token_binding: str | None = None
        self._tool_specs: dict[str, ToolSpec] | None = None

    # ------------------------------------------------------------------ #
    # Connection lifecycle                                               #
    # ------------------------------------------------------------------ #

    def connect(self, conn: MCPConnection) -> None:
        """Bind the client to ``conn``, enforcing RFC 8707 token binding."""
        if not isinstance(conn, MCPConnection):  # pragma: no cover - defensive
            raise MCPSecurityError("connect requires an MCPConnection")
        binding = token_binding(conn)
        if conn.auth.type.value != "none" and not binding:
            raise MCPSecurityError(
                f"authenticated connection {conn.id!r} has no RFC 8707 resource "
                "binding (set auth.resource or endpoint)"
            )
        self._connection = conn
        self._token_binding = binding
        self._tool_specs = None

    @property
    def connection(self) -> MCPConnection | None:
        return self._connection

    @property
    def token_binding(self) -> str | None:
        return self._token_binding

    @property
    def audit_entries(self) -> list[MCPAuditEntry]:
        if isinstance(self._audit, InMemoryAuditLog):
            return self._audit.entries
        return []

    def _require_connection(self) -> MCPConnection:
        if self._connection is None:
            raise MCPSecurityError("client is not connected; call connect() first")
        return self._connection

    # ------------------------------------------------------------------ #
    # Resources (namespace-scoped reads)                                 #
    # ------------------------------------------------------------------ #

    def list_resources(self, namespace: str | None = None) -> list[MCPResource]:
        conn = self._require_connection()
        # Rule 4: every live list is audited (redacted; secret-free payload hash).
        start = time.perf_counter()
        try:
            resources = self._transport.list_resources()
        except Exception:
            self._record(
                conn.id,
                "resources/list",
                {"namespace": namespace},
                "error",
                self._elapsed_ms(start),
            )
            raise
        self._record(
            conn.id,
            "resources/list",
            {"namespace": namespace},
            "ok",
            self._elapsed_ms(start),
        )
        return filter_resources(resources, conn.allowed_namespaces, requested=namespace)

    def read_resource(self, uri: str) -> MCPResourceContent:
        conn = self._require_connection()
        ns = namespace_of(uri)
        if not resource_in_scope(ns, conn.allowed_namespaces):
            # Rule 5: an out-of-scope read is refused *before* any byte reaches the
            # server, and the refusal is itself audited (spec MCP rules 4 & 5).
            self._record(conn.id, "resources/read", {"uri": uri}, "forbidden", None)
            raise MCPNamespaceError(
                f"resource {uri!r} (namespace {ns!r}) is outside the allowed "
                f"namespaces {conn.allowed_namespaces} for connection {conn.id!r}"
            )
        start = time.perf_counter()
        try:
            content = self._transport.read_resource(uri)
        except Exception:
            self._record(conn.id, "resources/read", {"uri": uri}, "error", self._elapsed_ms(start))
            raise
        self._record(conn.id, "resources/read", {"uri": uri}, "ok", self._elapsed_ms(start))
        return content.model_copy(update={"content": redact(content.content)})

    # ------------------------------------------------------------------ #
    # Tools (write-gated, policy-checked, audited)                       #
    # ------------------------------------------------------------------ #

    def _tool_spec(self, name: str) -> ToolSpec | None:
        if self._tool_specs is None:
            try:
                specs = self._transport.list_tools()
            except Exception:
                specs = []
            self._tool_specs = {s.name: s for s in specs}
        return self._tool_specs.get(name)

    def call_tool(self, name: str, arguments: dict[str, object]) -> MCPToolResult:
        conn = self._require_connection()

        # Rule 3: validate inputs before anything executes.
        if not isinstance(name, str) or not name.strip():
            raise MCPInputError("tool name must be a non-empty string")
        if not isinstance(arguments, Mapping):
            raise MCPInputError("tool arguments must be a mapping")
        arguments = dict(arguments)

        # Rule 1: write tools require an explicitly write-enabled connection.
        if is_write_tool(name, self._tool_spec(name)) and not conn.allow_write:
            self._record(conn.id, name, arguments, "forbidden", None)
            raise MCPWriteForbiddenError(
                f"tool {name!r} is a write operation but connection {conn.id!r} "
                "is read-only (allow_write=false)"
            )

        # Rule 7: same policy evaluation as any other agent tool call.
        self._policy_gate(conn, name, arguments)

        # Execute via the (mocked-in-tests) transport, measuring latency.
        start = time.perf_counter()
        try:
            raw = self._transport.call_tool(name, arguments)
        except Exception as exc:
            latency = self._elapsed_ms(start)
            self._record(conn.id, name, arguments, "error", latency)
            return MCPToolResult(
                tool=name,
                status="error",
                error=str(exc),
                latency_ms=latency,
                payload_hash=payload_hash(arguments),
            )

        latency = self._elapsed_ms(start)
        self._record(conn.id, name, arguments, "ok", latency)
        return MCPToolResult(
            tool=name,
            status="ok",
            content=redact(raw),  # rule 6: redact secrets from the result
            latency_ms=latency,
            payload_hash=payload_hash(arguments),
        )

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    def _policy_gate(self, conn: MCPConnection, name: str, arguments: dict[str, Any]) -> None:
        if self._evaluator is None or self._policy is None:
            return
        call = ToolCall(
            tool=name,
            arguments=arguments,
            resource=self._token_binding,
            connection_id=conn.id,
        )
        decision = self._evaluator.evaluate(call, self._policy)
        if decision.requires_approval:
            self._record(conn.id, name, arguments, "needs_approval", None)
            raise ApprovalRequiredError(decision.reason or f"tool {name!r} requires approval")
        if not decision.allowed:
            self._record(conn.id, name, arguments, "denied", None)
            raise PolicyViolationError(decision.reason or f"tool {name!r} denied by policy")

    def _record(
        self, connection_id: str, tool: str, arguments: Any, status: str, latency_ms: int | None
    ) -> None:
        self._audit.record(
            build_audit_entry(
                connection_id=connection_id,
                tool=tool,
                arguments=arguments,
                status=status,
                latency_ms=latency_ms,
            )
        )

    @staticmethod
    def _elapsed_ms(start: float) -> int:
        return int((time.perf_counter() - start) * 1000)


# Structural-conformance guard: fail type-check if the class drifts from the
# frozen contract (mirrors the runtime ``isinstance`` check in the tests).
_: MCPClient = MCPGatewayClient()


__all__ = ["MCPGatewayClient"]
