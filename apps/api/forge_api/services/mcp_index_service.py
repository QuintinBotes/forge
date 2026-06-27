"""F20 auto-provisioning / teardown / status for MCP sync-and-index sources.

When an admin flips an MCP connection to ``index_strategy = sync_and_index`` the
API idempotently provisions a linked ``knowledge_source`` (``kind=mcp``,
``sync_mode=sync_and_index``, ``config.mcp_connection_id=<slug>``) and enqueues a
full sync onto the worker's Celery queue; flipping away disables the source and
(by default) purges its persisted chunks + ledger so revoked external content
cannot keep serving. The connection itself lives in the in-memory
:class:`forge_mcp.MCPConnectionManager` (foundation), so everything here keys on
the connection *slug*, which is also what ``KnowledgeScope.mcp_sources`` carries.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from functools import lru_cache

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from forge_api.schemas.mcp import McpIndexStatus
from forge_contracts.enums import MCPIndexStrategy
from forge_db.models import KnowledgeSource, RetrievalChunk
from forge_db.models.enums import KnowledgeSourceKind, SyncMode
from forge_knowledge import index_status_counts, latest_run, purge_index

SessionFactory = Callable[[], Session] | sessionmaker[Session]

FULL_SYNC_TASK = "forge.knowledge.mcp_full_sync"


def _delete_on_disable() -> bool:
    return os.environ.get("MCP_INDEX_DELETE_ON_DISABLE", "true").lower() not in (
        "0",
        "false",
        "no",
    )


def _source_uri(slug: str) -> str:
    return f"mcp://{slug}"


def find_indexed_source(
    factory: SessionFactory, *, slug: str, workspace_id: uuid.UUID
) -> KnowledgeSource | None:
    """Resolve the provisioned MCP source for ``slug`` within a workspace."""
    with factory() as session:
        return session.scalar(
            select(KnowledgeSource).where(
                KnowledgeSource.workspace_id == workspace_id,
                KnowledgeSource.kind == KnowledgeSourceKind.MCP,
                KnowledgeSource.uri == _source_uri(slug),
            )
        )


def ensure_indexed_source(
    factory: SessionFactory,
    *,
    slug: str,
    workspace_id: uuid.UUID,
    allowed_namespaces: list[str],
    freshness_sla_minutes: int | None,
) -> uuid.UUID:
    """Idempotently create/refresh the linked ``mcp_resource`` knowledge source."""
    with factory() as session:
        source = session.scalar(
            select(KnowledgeSource).where(
                KnowledgeSource.workspace_id == workspace_id,
                KnowledgeSource.kind == KnowledgeSourceKind.MCP,
                KnowledgeSource.uri == _source_uri(slug),
            )
        )
        config = {
            "mcp_connection_id": slug,
            "index_strategy": MCPIndexStrategy.SYNC_AND_INDEX.value,
            "allowed_namespaces": list(allowed_namespaces),
        }
        if source is None:
            source = KnowledgeSource(
                workspace_id=workspace_id,
                kind=KnowledgeSourceKind.MCP,
                name=slug,
                uri=_source_uri(slug),
                sync_mode=SyncMode.SYNC_AND_INDEX,
                freshness_sla_minutes=freshness_sla_minutes,
                config={**config, "index_status": "pending"},
            )
            session.add(source)
        else:
            existing_status = (source.config or {}).get("index_status")
            status = "pending" if existing_status in (None, "disabled") else existing_status
            source.sync_mode = SyncMode.SYNC_AND_INDEX
            source.freshness_sla_minutes = freshness_sla_minutes
            source.config = {**config, "index_status": status}
        session.flush()
        source_id = source.id
        session.commit()
    return source_id


def teardown_indexed_source(
    factory: SessionFactory, *, slug: str, workspace_id: uuid.UUID, purge: bool | None = None
) -> None:
    """Disable the linked source; purge its chunks + ledger when configured."""
    purge = _delete_on_disable() if purge is None else purge
    source = find_indexed_source(factory, slug=slug, workspace_id=workspace_id)
    if source is None:
        return
    source_id = source.id
    if purge:
        with factory() as session:
            session.execute(
                delete(RetrievalChunk).where(RetrievalChunk.knowledge_source_id == source_id)
            )
            session.commit()
        purge_index(factory, source_id)
    with factory() as session:
        live = session.get(KnowledgeSource, source_id)
        if live is not None:
            live.config = {**(live.config or {}), "index_status": "disabled"}
            session.commit()


def index_status(
    factory: SessionFactory,
    *,
    slug: str,
    workspace_id: uuid.UUID,
    index_strategy: MCPIndexStrategy,
    now: datetime | None = None,
) -> McpIndexStatus:
    """Project the index status for a connection's provisioned source."""
    moment = now or datetime.now(UTC)
    source = find_indexed_source(factory, slug=slug, workspace_id=workspace_id)
    if source is None:
        default_status = (
            "pending" if index_strategy is MCPIndexStrategy.SYNC_AND_INDEX else "disabled"
        )
        return McpIndexStatus(index_strategy=index_strategy, status=default_status)

    resource_count, chunk_count = index_status_counts(factory, source.id)
    run = latest_run(factory, source.id)
    sla = source.freshness_sla_minutes or 30
    stale = _is_stale(source.last_synced_at, sla, moment)
    return McpIndexStatus(
        source_id=source.id,
        index_strategy=index_strategy,
        status=(source.config or {}).get("index_status", "pending"),
        resource_count=resource_count,
        chunk_count=chunk_count,
        last_synced_at=source.last_synced_at,
        freshness_sla_minutes=sla,
        stale=stale,
        last_sync_run=_run_to_dict(run),
    )


def _is_stale(last_synced_at: datetime | None, sla_minutes: int, now: datetime) -> bool:
    if last_synced_at is None:
        return True
    last = last_synced_at if last_synced_at.tzinfo else last_synced_at.replace(tzinfo=UTC)
    return (now - last).total_seconds() > sla_minutes * 60


def _run_to_dict(run: object | None) -> dict | None:
    if run is None:
        return None
    return {
        "id": str(run.id),  # type: ignore[attr-defined]
        "mode": run.mode.value,  # type: ignore[attr-defined]
        "status": run.status.value,  # type: ignore[attr-defined]
        "resources_seen": run.resources_seen,  # type: ignore[attr-defined]
        "resources_indexed": run.resources_indexed,  # type: ignore[attr-defined]
        "resources_skipped": run.resources_skipped,  # type: ignore[attr-defined]
        "resources_deleted": run.resources_deleted,  # type: ignore[attr-defined]
        "chunks_indexed": run.chunks_indexed,  # type: ignore[attr-defined]
        "chunks_deleted": run.chunks_deleted,  # type: ignore[attr-defined]
        "sweep_skipped": run.sweep_skipped,  # type: ignore[attr-defined]
        "error": run.error,  # type: ignore[attr-defined]
        "finished_at": (
            run.finished_at.isoformat() if run.finished_at else None  # type: ignore[attr-defined]
        ),
    }


@lru_cache(maxsize=1)
def _celery_app() -> object:
    from celery import Celery

    url = os.environ.get("FORGE_REDIS_URL", "redis://localhost:6379/0")
    return Celery("forge-api-enqueue", broker=url, backend=url)


def enqueue_full_sync(source_id: uuid.UUID | str) -> None:
    """Enqueue a full MCP sync onto the worker queue (monkeypatched in tests)."""
    _celery_app().send_task(FULL_SYNC_TASK, args=[str(source_id)])  # type: ignore[attr-defined]


__all__ = [
    "FULL_SYNC_TASK",
    "enqueue_full_sync",
    "ensure_indexed_source",
    "find_indexed_source",
    "index_status",
    "teardown_indexed_source",
]
