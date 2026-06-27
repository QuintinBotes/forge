"""SQLAlchemy persistence for the F20 MCP sync ledger + run history.

Implements the :class:`~forge_knowledge.mcp_indexer.ResourceLedger` and
:class:`~forge_knowledge.mcp_indexer.SyncRunRecorder` protocols over the
``mcp_indexed_resource`` / ``knowledge_sync_run`` tables, plus small read helpers
(``index_status_counts`` / ``latest_run`` / ``purge_index``) the API uses for the
index-status endpoint and switch-away purge. Centralised here (knowledge-core
already owns the SQLAlchemy stores) so the worker task and the API service share
one implementation rather than duplicating it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, or_, select
from sqlalchemy.orm import Session

from forge_db.models import KnowledgeSource, KnowledgeSyncRun, MCPIndexedResource
from forge_db.models.enums import RunStatus, SyncMode
from forge_knowledge.mcp_indexer import LedgerRow, SyncDirection, SyncReport

__all__ = [
    "SqlResourceLedger",
    "SqlSyncRunRecorder",
    "index_status_counts",
    "latest_run",
    "purge_index",
]

SessionFactory = Callable[[], Session]

_DIRECTION_TO_MODE = {
    SyncDirection.full: SyncMode.FULL,
    SyncDirection.incremental: SyncMode.INCREMENTAL,
}


def _now() -> datetime:
    return datetime.now(UTC)


def _to_row(model: MCPIndexedResource) -> LedgerRow:
    return LedgerRow(
        knowledge_source_id=model.knowledge_source_id,
        resource_uri=model.resource_uri,
        connection_slug=model.connection_slug,
        mcp_connection_id=model.mcp_connection_id,
        namespace=model.namespace,
        title=model.title,
        mime_type=model.mime_type,
        change_token=model.change_token,
        content_hash=model.content_hash,
        chunk_count=model.chunk_count,
        byte_size=model.byte_size,
        last_indexed_at=model.last_indexed_at,
        deleted_at=model.deleted_at,
    )


class SqlResourceLedger:
    """``ResourceLedger`` backed by ``mcp_indexed_resource``."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        self._workspace_cache: dict[uuid.UUID, uuid.UUID] = {}

    def _workspace_id(self, session: Session, source_id: uuid.UUID) -> uuid.UUID:
        cached = self._workspace_cache.get(source_id)
        if cached is not None:
            return cached
        source = session.get(KnowledgeSource, source_id)
        if source is None:
            raise LookupError(f"knowledge source {source_id} not found")
        self._workspace_cache[source_id] = source.workspace_id
        return source.workspace_id

    def get(self, source_id: uuid.UUID, resource_uri: str) -> LedgerRow | None:
        with self._session_factory() as session:
            model = session.scalar(
                select(MCPIndexedResource).where(
                    MCPIndexedResource.knowledge_source_id == source_id,
                    MCPIndexedResource.resource_uri == resource_uri,
                )
            )
            return _to_row(model) if model is not None else None

    def upsert(self, row: LedgerRow, *, sync_run_id: uuid.UUID) -> None:
        with self._session_factory() as session:
            model = session.scalar(
                select(MCPIndexedResource).where(
                    MCPIndexedResource.knowledge_source_id == row.knowledge_source_id,
                    MCPIndexedResource.resource_uri == row.resource_uri,
                )
            )
            if model is None:
                model = MCPIndexedResource(
                    workspace_id=self._workspace_id(session, row.knowledge_source_id),
                    knowledge_source_id=row.knowledge_source_id,
                    resource_uri=row.resource_uri,
                )
                session.add(model)
            model.connection_slug = row.connection_slug
            model.mcp_connection_id = row.mcp_connection_id
            model.namespace = row.namespace
            model.title = row.title
            model.mime_type = row.mime_type
            model.change_token = row.change_token
            # Order matters for the CHECK constraint: clear the tombstone only
            # alongside a non-zero chunk count (a re-indexed resource is live).
            model.chunk_count = row.chunk_count
            model.deleted_at = None
            model.content_hash = row.content_hash
            model.byte_size = row.byte_size
            model.last_indexed_at = row.last_indexed_at or _now()
            model.last_seen_sync_run_id = sync_run_id
            session.commit()

    def mark_seen(self, source_id: uuid.UUID, resource_uri: str, *, sync_run_id: uuid.UUID) -> None:
        with self._session_factory() as session:
            model = session.scalar(
                select(MCPIndexedResource).where(
                    MCPIndexedResource.knowledge_source_id == source_id,
                    MCPIndexedResource.resource_uri == resource_uri,
                )
            )
            if model is not None:
                model.last_seen_sync_run_id = sync_run_id
                session.commit()

    def tombstone_unseen(self, source_id: uuid.UUID, *, sync_run_id: uuid.UUID) -> list[str]:
        with self._session_factory() as session:
            stale = list(
                session.scalars(
                    select(MCPIndexedResource).where(
                        MCPIndexedResource.knowledge_source_id == source_id,
                        MCPIndexedResource.deleted_at.is_(None),
                        or_(
                            MCPIndexedResource.last_seen_sync_run_id != sync_run_id,
                            MCPIndexedResource.last_seen_sync_run_id.is_(None),
                        ),
                    )
                )
            )
            uris: list[str] = []
            now = _now()
            for model in stale:
                model.chunk_count = 0  # satisfy CHECK before setting deleted_at
                model.deleted_at = now
                uris.append(model.resource_uri)
            session.commit()
            return uris

    def purge_source(self, source_id: uuid.UUID) -> int:
        with self._session_factory() as session:
            result = cast(
                "CursorResult[Any]",
                session.execute(
                    delete(MCPIndexedResource).where(
                        MCPIndexedResource.knowledge_source_id == source_id
                    )
                ),
            )
            session.commit()
            return int(result.rowcount or 0)


class SqlSyncRunRecorder:
    """``SyncRunRecorder`` backed by ``knowledge_sync_run``."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def open(
        self, *, source_id: uuid.UUID, workspace_id: uuid.UUID, mode: SyncDirection
    ) -> uuid.UUID:
        with self._session_factory() as session:
            run = KnowledgeSyncRun(
                workspace_id=workspace_id,
                knowledge_source_id=source_id,
                mode=_DIRECTION_TO_MODE[mode],
                status=RunStatus.RUNNING,
                started_at=_now(),
            )
            session.add(run)
            session.commit()
            return run.id

    def close(self, report: SyncReport) -> None:
        with self._session_factory() as session:
            run = session.get(KnowledgeSyncRun, report.sync_run_id)
            if run is None:
                return
            run.status = RunStatus.FAILED if report.error else RunStatus.SUCCEEDED
            run.resources_seen = report.resources_seen
            run.resources_indexed = report.resources_indexed
            run.resources_skipped = report.resources_skipped
            run.resources_deleted = report.resources_deleted
            run.chunks_indexed = report.chunks_indexed
            run.chunks_deleted = report.chunks_deleted
            run.chunks_skipped = report.chunks_skipped
            run.sweep_skipped = report.sweep_skipped
            run.cap_hit = report.cap_hit
            run.error = report.error
            run.finished_at = report.finished_at or _now()
            session.commit()


# --------------------------------------------------------------------------- #
# Read helpers (API status / purge)                                           #
# --------------------------------------------------------------------------- #


def index_status_counts(session_factory: SessionFactory, source_id: uuid.UUID) -> tuple[int, int]:
    """Return ``(live_resource_count, total_chunk_count)`` for a source."""
    with session_factory() as session:
        resource_count = (
            session.scalar(
                select(func.count())
                .select_from(MCPIndexedResource)
                .where(
                    MCPIndexedResource.knowledge_source_id == source_id,
                    MCPIndexedResource.deleted_at.is_(None),
                )
            )
            or 0
        )
        chunk_count = (
            session.scalar(
                select(func.coalesce(func.sum(MCPIndexedResource.chunk_count), 0)).where(
                    MCPIndexedResource.knowledge_source_id == source_id,
                    MCPIndexedResource.deleted_at.is_(None),
                )
            )
            or 0
        )
        return int(resource_count), int(chunk_count)


def latest_run(session_factory: SessionFactory, source_id: uuid.UUID) -> KnowledgeSyncRun | None:
    """Return the most recently started sync run for a source, if any."""
    with session_factory() as session:
        return session.scalar(
            select(KnowledgeSyncRun)
            .where(KnowledgeSyncRun.knowledge_source_id == source_id)
            .order_by(
                KnowledgeSyncRun.started_at.desc().nullslast(),
                KnowledgeSyncRun.created_at.desc(),
            )
            .limit(1)
        )


def purge_index(session_factory: SessionFactory, source_id: uuid.UUID) -> int:
    """Delete every ledger row for a source (chunks are purged via the store)."""
    return SqlResourceLedger(session_factory).purge_source(source_id)
