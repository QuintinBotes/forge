"""MCP client manager gateway service for Forge.

Wraps the :mod:`forge_mcp` SDK in a FastAPI service that registers MCP
connections and serves audited, read-only-by-default, namespace-scoped access to
their resources and tools.
"""

from __future__ import annotations

__version__ = "0.1.0"
