"""Adapters wiring worker tasks to external SDKs (F20: MCP gateway fetcher)."""

from __future__ import annotations

from forge_worker.adapters.gateway_fetcher import GatewayMcpResourceFetcher

__all__ = ["GatewayMcpResourceFetcher"]
