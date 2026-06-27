"""Transport abstraction for talking to a live MCP server (plan Task 1.12).

The :class:`Transport` protocol is the seam between this SDK and the wire: the
real implementation speaks the MCP protocol over http/stdio/sse, while tests and
the gateway inject a fixture-backed fake (:class:`forge_mcp.testing.FakeTransport`).
The client itself never performs I/O — it orchestrates security, audit, and
policy around whatever transport it is given, so no live call ever happens
implicitly.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from forge_contracts import MCPResource, MCPResourceContent
from forge_mcp.exceptions import MCPTransportUnavailableError


class ToolSpec(BaseModel):
    """A tool advertised by an MCP server, with MCP 2025 safety annotations.

    ``read_only`` mirrors the MCP ``readOnlyHint`` annotation: ``True`` means the
    tool does not modify its environment, ``False`` means it may, and ``None``
    means the server gave no hint (the name heuristic then decides).
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    name: str
    description: str | None = None
    read_only: bool | None = None
    destructive: bool | None = None
    annotations: dict[str, Any] = {}


@runtime_checkable
class Transport(Protocol):
    """The minimal surface the client needs from a live MCP connection."""

    def list_resources(self) -> list[MCPResource]: ...

    def read_resource(self, uri: str) -> MCPResourceContent: ...

    def list_tools(self) -> list[ToolSpec]: ...

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any: ...


class NullTransport:
    """Default transport that refuses to perform live MCP traffic.

    Used when no transport is injected. It allows a client/connection to exist
    (so registration and metadata operations work) but raises on any operation
    that would require a live server — enforcing "no real external calls".
    """

    def list_resources(self) -> list[MCPResource]:
        raise MCPTransportUnavailableError(
            "no live MCP transport configured (inject a Transport to enable live calls)"
        )

    def read_resource(self, uri: str) -> MCPResourceContent:
        raise MCPTransportUnavailableError("no live MCP transport configured")

    def list_tools(self) -> list[ToolSpec]:
        return []

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any:
        raise MCPTransportUnavailableError("no live MCP transport configured")


__all__ = ["NullTransport", "ToolSpec", "Transport"]
