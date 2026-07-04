"""MCP client SDK: connection model, query-through retrieval, audit, security.

Implements the frozen :class:`forge_contracts.MCPClient` contract plus the
spec's MCP Security Rules (read-only default, RFC 8707 token binding, namespace
scoping, redacted audit log) and the query-through retrieval path. The live
transport is pluggable; tests and the gateway inject
:class:`forge_mcp.testing.FakeTransport`.
"""

from __future__ import annotations

from forge_mcp.audit import AuditSink, InMemoryAuditLog, build_audit_entry
from forge_mcp.client import MCPGatewayClient
from forge_mcp.connection import load_connection, load_connection_file
from forge_mcp.exceptions import (
    MCPConnectionNotFoundError,
    MCPError,
    MCPInputError,
    MCPNamespaceError,
    MCPSecurityError,
    MCPTransportUnavailableError,
)
from forge_mcp.manager import MCPConnectionManager, TransportFactory
from forge_mcp.query_through import query_through
from forge_mcp.security import (
    SENSITIVE_KEYS,
    WRITE_KEYWORDS,
    filter_resources,
    is_write_tool,
    namespace_of,
    payload_hash,
    redact,
    resource_in_scope,
    token_binding,
)
from forge_mcp.transport import NullTransport, ToolSpec, Transport

__version__ = "0.1.0"

__all__ = [
    "SENSITIVE_KEYS",
    "WRITE_KEYWORDS",
    "AuditSink",
    "InMemoryAuditLog",
    "MCPConnectionManager",
    "MCPConnectionNotFoundError",
    "MCPError",
    "MCPGatewayClient",
    "MCPInputError",
    "MCPNamespaceError",
    "MCPSecurityError",
    "MCPTransportUnavailableError",
    "NullTransport",
    "ToolSpec",
    "Transport",
    "TransportFactory",
    "build_audit_entry",
    "filter_resources",
    "is_write_tool",
    "load_connection",
    "load_connection_file",
    "namespace_of",
    "payload_hash",
    "query_through",
    "redact",
    "resource_in_scope",
    "token_binding",
]
