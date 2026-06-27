"""MCP connection manager — re-exported from the SDK (plan Task 1.12).

The connection manager is reusable control-plane logic shared by the gateway
service and the API ``/mcp/*`` router, so it lives in the ``forge_mcp`` SDK
(:mod:`forge_mcp.manager`). This module re-exports it for backward compatibility
with ``from forge_mcp_gateway.manager import MCPConnectionManager`` call sites.
"""

from __future__ import annotations

from forge_mcp.manager import MCPConnectionManager, TransportFactory

__all__ = ["MCPConnectionManager", "TransportFactory"]
