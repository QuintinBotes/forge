"""SDK-local exceptions for the MCP client (plan Task 1.12).

These extend the shared :class:`forge_contracts.ForgeError` hierarchy so callers
can catch a stable base type. The write-forbidden case reuses the frozen
:class:`forge_contracts.MCPWriteForbiddenError` directly (spec rule 1).
"""

from __future__ import annotations

from forge_contracts import ForgeError


class MCPError(ForgeError):
    """Base class for MCP-client errors raised by this SDK."""


class MCPSecurityError(MCPError):
    """A connection violates a security precondition (e.g. unbound token)."""


class MCPNamespaceError(MCPError):
    """A resource lies outside a connection's allowed namespaces (rule 5)."""


class MCPInputError(MCPError):
    """A tool call's inputs failed validation before execution (rule 3)."""


class MCPTransportUnavailableError(MCPError):
    """No live transport is configured for an operation that requires one.

    Raised by the default (null) transport so that live MCP traffic never
    happens implicitly; callers inject a real or fake transport explicitly.
    """


class MCPConnectionNotFoundError(MCPError, KeyError):
    """A requested MCP connection id is not registered with the manager."""


__all__ = [
    "MCPConnectionNotFoundError",
    "MCPError",
    "MCPInputError",
    "MCPNamespaceError",
    "MCPSecurityError",
    "MCPTransportUnavailableError",
]
