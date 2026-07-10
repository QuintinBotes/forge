"""SDK-local exceptions for the MCP client (plan Task 1.12).

These extend the shared :class:`forge_contracts.ForgeError` hierarchy so callers
can catch a stable base type. The write-forbidden case reuses the frozen
:class:`forge_contracts.MCPWriteForbiddenError` directly (spec rule 1).
"""

from __future__ import annotations

from typing import Any

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


class MCPRateLimitedError(MCPError):
    """A per-connection rate-limit budget was exhausted (F40 delta 4).

    Deliberately a *typed, retryable* error — NOT a tool/run failure. The client
    raises it (rather than returning an ``MCPToolResult(status="error")``) before
    any live traffic happens, so the caller can back off and retry once
    ``retry_after_s`` has elapsed. The control plane maps it to ``429``.
    """

    def __init__(self, message: str, *, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class MCPElicitationRequiredError(MCPError):
    """The MCP server requested *elicitation* (more input) mid tool-call (delta 3).

    Server-initiated elicitation is surfaced to the human approver rather than
    silently auto-answered: it carries the server-provided ``message`` and the
    requested JSON ``schema`` so the approver can supply the missing input. The
    control plane maps it to ``428 Precondition Required``.
    """

    def __init__(self, message: str, *, schema: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.elicit_message = message
        self.schema = schema or {}


__all__ = [
    "MCPConnectionNotFoundError",
    "MCPElicitationRequiredError",
    "MCPError",
    "MCPInputError",
    "MCPNamespaceError",
    "MCPRateLimitedError",
    "MCPSecurityError",
    "MCPTransportUnavailableError",
]
