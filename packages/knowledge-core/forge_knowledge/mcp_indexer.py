"""MCP sync-and-index pipeline (F20): list -> read -> chunk -> redact -> upsert.

:class:`McpSyncIndexer` composes F09 (the read-only gateway, via the
:class:`McpResourceFetcher` adapter) and F05 (chunking + the ``retrieval_chunk``
upsert store) into the periodic ingestion pipeline. It is engine-agnostic and
pure with respect to I/O: every boundary is a :class:`typing.Protocol`
(``fetcher`` / ``chunker`` / ``indexer`` store / ``ledger`` / ``runs``) so the
whole pipeline is unit-testable with in-memory fakes and reused identically from
a Celery task.

Deviation from the slice doc: the foundation's stores/service are *synchronous*
(``KnowledgeService.index`` etc. are sync, Celery tasks are sync), so the
protocols here are synchronous too (the slice sketched ``async``). The slice also
keyed the ledger on ``connection_id`` (UUID); we key on ``connection_slug`` to
match the in-memory connection identity used everywhere in the foundation.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, Field

from forge_contracts.dtos import Chunk, IndexResult
from forge_knowledge.mcp_chunking import (
    McpResourceSnapshot,
    provenance_uri,
)
from forge_knowledge.redaction import redact_secrets

__all__ = [
    "ChunkIndexStore",
    "LedgerRow",
    "McpResourceChunkerProto",
    "McpResourceFetcher",
    "McpSyncIndexer",
    "ResourceLedger",
    "ResourceRef",
    "SyncDirection",
    "SyncReport",
    "SyncRunRecorder",
]

#: Default safety / throughput knobs (overridable via env in the worker).
DEFAULT_PAGE_SIZE = 100
DEFAULT_MAX_RESOURCES = 5000


def _now() -> datetime:
    return datetime.now(UTC)


class SyncDirection(StrEnum):
    full = "full"
    incremental = "incremental"


class ResourceRef(BaseModel):
    """A resource enumerated from ``resources/list`` (server-side metadata only)."""

    uri: str
    namespace: str | None = None
    title: str | None = None
    mime_type: str | None = None
    change_token: str | None = None
    url: str | None = None


class LedgerRow(BaseModel):
    """The per-resource ledger record (mirror of ``mcp_indexed_resource``)."""

    knowledge_source_id: UUID
    resource_uri: str
    connection_slug: str
    mcp_connection_id: UUID | None = None
    namespace: str | None = None
    title: str | None = None
    mime_type: str | None = None
    change_token: str | None = None
    content_hash: str = ""
    chunk_count: int = 0
    byte_size: int = 0
    last_indexed_at: datetime | None = None
    deleted_at: datetime | None = None


class SyncReport(BaseModel):
    """Per-run reconciliation counters (mirror of ``knowledge_sync_run``)."""

    knowledge_source_id: UUID
    sync_run_id: UUID
    mode: SyncDirection
    resources_seen: int = 0
    resources_indexed: int = 0
    resources_skipped: int = 0
    resources_deleted: int = 0
    chunks_indexed: int = 0
    chunks_deleted: int = 0
    chunks_skipped: int = 0
    sweep_skipped: bool = False
    cap_hit: bool = False
    started_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None
    error: str | None = None


# --------------------------------------------------------------------------- #
# Boundary protocols (fakeable in tests, SQLAlchemy/gateway impls in worker)   #
# --------------------------------------------------------------------------- #


class McpResourceFetcher(Protocol):
    """Read-only adapter over the F09 MCP gateway (namespace-scoped + audited)."""

    def list_resources(
        self, *, namespaces: list[str] | None, cursor: str | None
    ) -> tuple[list[ResourceRef], str | None]: ...

    def read_resource(self, uri: str) -> McpResourceSnapshot: ...


class McpResourceChunkerProto(Protocol):
    def chunk(self, snapshot: McpResourceSnapshot) -> list[Chunk]: ...


class ChunkIndexStore(Protocol):
    """The F05 upsert surface (``KnowledgeService`` / ``PgVectorStore`` satisfy it)."""

    def index(self, source_id: str, chunks: list[Chunk]) -> IndexResult: ...

    def delete_source_paths(self, source_id: str, paths: Iterable[str]) -> int: ...


class ResourceLedger(Protocol):
    def get(self, source_id: UUID, resource_uri: str) -> LedgerRow | None: ...

    def upsert(self, row: LedgerRow, *, sync_run_id: UUID) -> None: ...

    def mark_seen(self, source_id: UUID, resource_uri: str, *, sync_run_id: UUID) -> None: ...

    def tombstone_unseen(self, source_id: UUID, *, sync_run_id: UUID) -> list[str]: ...

    def purge_source(self, source_id: UUID) -> int: ...


class SyncRunRecorder(Protocol):
    def open(self, *, source_id: UUID, workspace_id: UUID, mode: SyncDirection) -> UUID: ...

    def close(self, report: SyncReport) -> None: ...


# --------------------------------------------------------------------------- #
# Indexer                                                                      #
# --------------------------------------------------------------------------- #


class McpSyncIndexer:
    """Full / incremental MCP resource sync into the local hybrid index."""

    def __init__(
        self,
        *,
        fetcher: McpResourceFetcher,
        chunker: McpResourceChunkerProto,
        indexer: ChunkIndexStore,
        ledger: ResourceLedger,
        runs: SyncRunRecorder,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_resources: int = DEFAULT_MAX_RESOURCES,
    ) -> None:
        self._fetcher = fetcher
        self._chunker = chunker
        self._indexer = indexer
        self._ledger = ledger
        self._runs = runs
        self._page_size = page_size
        self._max_resources = max_resources

    def sync(
        self,
        *,
        source_id: UUID,
        workspace_id: UUID,
        connection_slug: str,
        allowed_namespaces: list[str] | None,
        mode: SyncDirection,
        mcp_connection_id: UUID | None = None,
    ) -> SyncReport:
        run_id = self._runs.open(source_id=source_id, workspace_id=workspace_id, mode=mode)
        report = SyncReport(knowledge_source_id=source_id, sync_run_id=run_id, mode=mode)
        source_id_str = str(source_id)

        complete = True
        try:
            cursor: str | None = None
            while True:
                if report.resources_seen >= self._max_resources:
                    report.cap_hit = True
                    complete = False
                    break
                try:
                    refs, cursor = self._fetcher.list_resources(
                        namespaces=allowed_namespaces, cursor=cursor
                    )
                except Exception:  # partial enumeration -> skip sweep, keep chunks
                    complete = False
                    break

                hit_cap = False
                for ref in refs:
                    if report.resources_seen >= self._max_resources:
                        report.cap_hit = True
                        complete = False
                        hit_cap = True
                        break
                    report.resources_seen += 1
                    self._process(
                        ref,
                        source_id=source_id,
                        source_id_str=source_id_str,
                        connection_slug=connection_slug,
                        mcp_connection_id=mcp_connection_id,
                        run_id=run_id,
                        report=report,
                    )

                if hit_cap or not cursor:
                    break

            if complete:
                self._sweep(source_id, source_id_str, connection_slug, run_id, report)
            else:
                report.sweep_skipped = True
        except Exception as exc:  # record the failure on the run row, never crash beat
            report.error = str(exc)
            report.sweep_skipped = True

        report.finished_at = _now()
        self._runs.close(report)
        return report

    # ------------------------------------------------------------------ #

    def _process(
        self,
        ref: ResourceRef,
        *,
        source_id: UUID,
        source_id_str: str,
        connection_slug: str,
        mcp_connection_id: UUID | None,
        run_id: UUID,
        report: SyncReport,
    ) -> None:
        existing = self._ledger.get(source_id, ref.uri)
        live = existing if (existing and existing.deleted_at is None) else None

        # Skip without reading when the server reports an unchanged change-token.
        if live and ref.change_token is not None and live.change_token == ref.change_token:
            report.resources_skipped += 1
            report.chunks_skipped += live.chunk_count
            self._ledger.mark_seen(source_id, ref.uri, sync_run_id=run_id)
            return

        snapshot = self._fetcher.read_resource(ref.uri)
        redacted = redact_secrets(snapshot.content)
        content_hash = _content_hash(connection_slug, ref.uri, redacted)

        # Fallback change detection: unchanged content -> no re-chunk / re-embed.
        if live and ref.change_token is None and live.content_hash == content_hash:
            report.resources_skipped += 1
            report.chunks_skipped += live.chunk_count
            self._ledger.mark_seen(source_id, ref.uri, sync_run_id=run_id)
            return

        snapshot = snapshot.model_copy(
            update={
                "content": redacted,
                "connection_slug": connection_slug,
                "namespace": snapshot.namespace or ref.namespace,
                "title": snapshot.title or ref.title,
                "url": snapshot.url or ref.url,
                "mime_type": snapshot.mime_type or ref.mime_type,
                "change_token": ref.change_token,
            }
        )
        chunks = self._chunker.chunk(snapshot)

        full_path = provenance_uri(connection_slug, ref.uri)
        if live and live.chunk_count > 0:
            report.chunks_deleted += self._indexer.delete_source_paths(source_id_str, [full_path])
        if chunks:
            result = self._indexer.index(source_id_str, chunks)
            report.chunks_indexed += result.indexed

        report.resources_indexed += 1
        self._ledger.upsert(
            LedgerRow(
                knowledge_source_id=source_id,
                resource_uri=ref.uri,
                connection_slug=connection_slug,
                mcp_connection_id=mcp_connection_id,
                namespace=snapshot.namespace,
                title=snapshot.title,
                mime_type=snapshot.mime_type,
                change_token=ref.change_token,
                content_hash=content_hash,
                chunk_count=len(chunks),
                byte_size=len(redacted.encode("utf-8")),
                last_indexed_at=_now(),
                deleted_at=None,
            ),
            sync_run_id=run_id,
        )

    def _sweep(
        self,
        source_id: UUID,
        source_id_str: str,
        connection_slug: str,
        run_id: UUID,
        report: SyncReport,
    ) -> None:
        purged_uris = self._ledger.tombstone_unseen(source_id, sync_run_id=run_id)
        for uri in purged_uris:
            full_path = provenance_uri(connection_slug, uri)
            report.chunks_deleted += self._indexer.delete_source_paths(source_id_str, [full_path])
            report.resources_deleted += 1


def _content_hash(connection_slug: str, resource_uri: str, redacted_content: str) -> str:
    import hashlib

    key = f"{connection_slug}\0{resource_uri}\0{redacted_content}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()
