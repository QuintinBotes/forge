"""F20 MCP sync-and-index Celery tasks (queue ``knowledge``).

* ``forge.knowledge.mcp_full_sync`` — full enumerate + reconcile.
* ``forge.knowledge.mcp_incremental_sync`` — skip unchanged by change-token/hash,
  still tombstone-sweep on a complete enumeration.
* ``forge.knowledge.refresh_stale_mcp_sources`` — beat: enqueue an incremental
  sync for every ``sync_and_index`` source older than its ``freshness_sla_minutes``.

The heavy lifting is :class:`~forge_knowledge.mcp_indexer.McpSyncIndexer`; the
tasks are thin and delegate to :func:`run_sync`, which is pure-ish (all I/O
boundaries injectable) so it is tested directly against Postgres without a live
Celery/Redis broker. ``build_mcp_fetcher`` defaults to a NullTransport-backed
gateway client (no live MCP traffic) — the real transport is injected at the
Phase-2 wire-up barrier, and tests override it with an in-memory fake.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select

from forge_db.models import KnowledgeSource
from forge_db.models.enums import KnowledgeSourceKind, SyncMode
from forge_knowledge import (
    McpResourceChunker,
    McpSyncIndexer,
    SqlResourceLedger,
    SqlSyncRunRecorder,
    SyncDirection,
)
from forge_knowledge.mcp_indexer import McpResourceFetcher, SyncReport
from forge_worker.adapters.gateway_fetcher import GatewayMcpResourceFetcher
from forge_worker.celery_app import celery_app
from forge_worker.indexer import build_knowledge_service

__all__ = [
    "build_mcp_fetcher",
    "mcp_full_sync",
    "mcp_full_sync_task",
    "mcp_incremental_sync",
    "mcp_incremental_sync_task",
    "refresh_stale_mcp_sources",
    "refresh_stale_mcp_sources_task",
    "run_sync",
]


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def poll_seconds() -> float:
    return float(os.environ.get("MCP_INDEX_POLL_SECONDS", "300"))


def build_mcp_fetcher(
    *, slug: str, allowed_namespaces: list[str], endpoint: str | None = None
) -> McpResourceFetcher:
    """Default fetcher: a NullTransport gateway client (no live MCP traffic).

    Overridden in tests (and at the live wire-up barrier) to inject a real or
    fake transport. Constructed lazily so importing this module stays hermetic.
    """
    from forge_contracts import MCPCapabilities, MCPConnection
    from forge_mcp.client import MCPGatewayClient

    conn = MCPConnection(
        id=slug,
        name=slug,
        endpoint=endpoint,
        allowed_namespaces=list(allowed_namespaces),
        capabilities=MCPCapabilities(resources=True),
    )
    client = MCPGatewayClient()
    client.connect(conn)
    return GatewayMcpResourceFetcher(client, connection_slug=slug)


def _source_descriptor(session_factory: Callable[[], Any], source_id: UUID) -> dict[str, Any]:
    with session_factory() as session:
        source = session.get(KnowledgeSource, source_id)
        if source is None:
            raise LookupError(f"knowledge source {source_id} not found")
        config = dict(source.config or {})
        return {
            "workspace_id": source.workspace_id,
            "slug": config.get("mcp_connection_id") or source.name,
            "allowed_namespaces": list(config.get("allowed_namespaces") or []),
            "endpoint": config.get("endpoint"),
        }


def _finalize_source(session_factory: Callable[[], Any], source_id: UUID, *, error: bool) -> None:
    with session_factory() as session:
        source = session.get(KnowledgeSource, source_id)
        if source is None:
            return
        source.config = {**(source.config or {}), "index_status": "error" if error else "ready"}
        source.last_synced_at = datetime.now(UTC)
        session.commit()


def run_sync(
    source_id: str,
    mode: SyncDirection,
    *,
    session_factory: Callable[[], Any] | None = None,
    store: Any | None = None,
    fetcher: McpResourceFetcher | None = None,
    chunker: McpResourceChunker | None = None,
) -> SyncReport:
    """Execute one MCP sync for ``source_id`` and persist its run + ledger rows."""
    from forge_db import create_session_factory

    factory = session_factory or create_session_factory()
    source_uuid = UUID(str(source_id))
    descriptor = _source_descriptor(factory, source_uuid)

    if fetcher is None:
        fetcher = build_mcp_fetcher(
            slug=descriptor["slug"],
            allowed_namespaces=descriptor["allowed_namespaces"],
            endpoint=descriptor["endpoint"],
        )
    store = store if store is not None else build_knowledge_service()

    indexer = McpSyncIndexer(
        fetcher=fetcher,
        chunker=chunker or McpResourceChunker(),
        indexer=store,
        ledger=SqlResourceLedger(factory),
        runs=SqlSyncRunRecorder(factory),
        page_size=_int_env("MCP_INDEX_PAGE_SIZE", 100),
        max_resources=_int_env("MCP_INDEX_MAX_RESOURCES", 5000),
    )
    report = indexer.sync(
        source_id=source_uuid,
        workspace_id=descriptor["workspace_id"],
        connection_slug=descriptor["slug"],
        allowed_namespaces=descriptor["allowed_namespaces"] or None,
        mode=mode,
    )
    _finalize_source(factory, source_uuid, error=report.error is not None)
    return report


def mcp_full_sync(source_id: str, **kwargs: Any) -> SyncReport:
    return run_sync(source_id, SyncDirection.full, **kwargs)


def mcp_incremental_sync(source_id: str, **kwargs: Any) -> SyncReport:
    return run_sync(source_id, SyncDirection.incremental, **kwargs)


# --------------------------------------------------------------------------- #
# Freshness beat                                                              #
# --------------------------------------------------------------------------- #


def _is_stale(source: KnowledgeSource, now: datetime) -> bool:
    if source.last_synced_at is None:
        return True
    last = source.last_synced_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    sla_minutes = source.freshness_sla_minutes or 30
    return (now - last).total_seconds() > sla_minutes * 60


def _enqueue_incremental(source_id: str) -> None:
    mcp_incremental_sync_task.delay(source_id)


def refresh_stale_mcp_sources(
    *,
    session_factory: Callable[[], Any] | None = None,
    enqueue: Callable[[str], None] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Enqueue an incremental sync for each stale, ready ``sync_and_index`` source."""
    from forge_db import create_session_factory

    factory = session_factory or create_session_factory()
    do_enqueue = enqueue or _enqueue_incremental
    moment = now or datetime.now(UTC)

    enqueued: list[str] = []
    with factory() as session:
        sources = list(
            session.scalars(
                select(KnowledgeSource).where(
                    KnowledgeSource.kind == KnowledgeSourceKind.MCP,
                    KnowledgeSource.sync_mode == SyncMode.SYNC_AND_INDEX,
                )
            )
        )

    for source in sources:
        try:
            if (source.config or {}).get("index_status") != "ready":
                continue
            if _is_stale(source, moment):
                do_enqueue(str(source.id))
                enqueued.append(str(source.id))
        except Exception:  # isolate per-source failures; never abort the batch
            continue
    return {"enqueued": enqueued, "scanned": len(sources)}


# --------------------------------------------------------------------------- #
# Celery task wrappers                                                        #
# --------------------------------------------------------------------------- #


@celery_app.task(name="forge.knowledge.mcp_full_sync", queue="knowledge")
def mcp_full_sync_task(source_id: str) -> dict[str, Any]:
    return mcp_full_sync(source_id).model_dump(mode="json")


@celery_app.task(name="forge.knowledge.mcp_incremental_sync", queue="knowledge")
def mcp_incremental_sync_task(source_id: str) -> dict[str, Any]:
    return mcp_incremental_sync(source_id).model_dump(mode="json")


@celery_app.task(name="forge.knowledge.refresh_stale_mcp_sources", queue="knowledge")
def refresh_stale_mcp_sources_task() -> dict[str, Any]:
    return refresh_stale_mcp_sources()
