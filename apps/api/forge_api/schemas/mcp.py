"""MCP control-plane request/response schemas (F20 extends F09).

F09 registered connections via the frozen ``MCPConnection`` contract and had no
post-create mutation surface. F20 adds:

* :class:`UpdateConnectionRequest` — a partial PATCH body whose only F20-relevant
  field is ``index_strategy`` (flip between ``query_through`` and
  ``sync_and_index``); ``allow_write`` is intentionally absent (immutable false).
* :class:`McpIndexStatus` — the index-status projection for the connection card.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from forge_contracts.enums import MCPIndexStrategy


class UpdateConnectionRequest(BaseModel):
    """Partial update for an MCP connection (F20: ``index_strategy`` flip)."""

    name: str | None = None
    allowed_namespaces: list[str] | None = None
    index_strategy: MCPIndexStrategy | None = None
    # ``allow_write`` is deliberately not accepted: it stays immutable-false.


class McpIndexStatus(BaseModel):
    """Index status for a connection's provisioned sync-and-index source."""

    source_id: uuid.UUID | None = None
    index_strategy: MCPIndexStrategy
    status: str = "disabled"  # pending | indexing | ready | error | disabled
    resource_count: int = 0
    chunk_count: int = 0
    last_synced_at: datetime | None = None
    freshness_sla_minutes: int = 30
    stale: bool = False
    last_sync_run: dict | None = None


__all__ = ["McpIndexStatus", "UpdateConnectionRequest"]
