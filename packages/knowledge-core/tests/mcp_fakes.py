"""In-memory fakes for the F20 MCP sync-and-index pipeline tests.

No DB, no network: the indexer's four boundaries (fetcher / chunker / store /
ledger+runs) are all fakeable, so the pipeline's incremental correctness is
tested purely. ``FakeMcpResourceFetcher`` counts ``read_resource`` calls per uri
(so skip-behaviour is assertable) and supports mutate / remove / partial-
enumeration modes.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

from forge_contracts.dtos import Chunk, IndexResult
from forge_knowledge.mcp_chunking import McpResourceSnapshot
from forge_knowledge.mcp_indexer import LedgerRow, ResourceRef, SyncDirection, SyncReport

SLUG = "confluence-engineering"


@dataclass
class FakeResource:
    uri: str
    body: str
    namespace: str | None = "engineering"
    title: str | None = None
    mime_type: str | None = "text/markdown"
    change_token: str | None = None


class FakeMcpResourceFetcher:
    """In-memory ``McpResourceFetcher`` with read-call counting + mutation hooks."""

    def __init__(
        self,
        resources: list[FakeResource],
        *,
        page_size: int | None = None,
        fail_mid_enumeration: bool = False,
    ) -> None:
        self._resources: list[FakeResource] = list(resources)
        self._page_size = page_size
        self._fail_mid = fail_mid_enumeration
        self.read_counts: Counter[str] = Counter()
        self.list_calls = 0

    # -- mutation helpers ------------------------------------------------ #

    def mutate(self, uri: str, *, body: str, change_token: str | None = None) -> None:
        for r in self._resources:
            if r.uri == uri:
                r.body = body
                r.change_token = change_token
                return
        raise KeyError(uri)

    def remove(self, uri: str) -> None:
        self._resources = [r for r in self._resources if r.uri != uri]

    # -- protocol -------------------------------------------------------- #

    def list_resources(
        self, *, namespaces: list[str] | None, cursor: str | None
    ) -> tuple[list[ResourceRef], str | None]:
        self.list_calls += 1
        if self._fail_mid and cursor is not None:
            raise RuntimeError("enumeration aborted mid-cursor")
        refs = [
            ResourceRef(
                uri=r.uri,
                namespace=r.namespace,
                title=r.title,
                mime_type=r.mime_type,
                change_token=r.change_token,
            )
            for r in self._resources
        ]
        if self._page_size is None:
            return refs, None
        start = int(cursor or 0)
        page = refs[start : start + self._page_size]
        nxt = start + self._page_size
        next_cursor = str(nxt) if nxt < len(refs) else None
        return page, next_cursor

    def read_resource(self, uri: str) -> McpResourceSnapshot:
        self.read_counts[uri] += 1
        for r in self._resources:
            if r.uri == uri:
                return McpResourceSnapshot(
                    uri=r.uri,
                    content=r.body,
                    connection_slug=SLUG,
                    mime_type=r.mime_type,
                    namespace=r.namespace,
                    title=r.title,
                    change_token=r.change_token,
                )
        raise KeyError(uri)


class FakeChunkStore:
    """In-memory ``ChunkIndexStore`` tracking chunks-by-path + index/embed calls."""

    def __init__(self) -> None:
        self.by_path: dict[str, list[Chunk]] = {}
        self.index_calls: list[tuple[str, list[Chunk]]] = []
        self.embed_count = 0

    def index(self, source_id: str, chunks: list[Chunk]) -> IndexResult:
        self.index_calls.append((source_id, list(chunks)))
        for chunk in chunks:
            self.by_path.setdefault(chunk.path or "", []).append(chunk)
            self.embed_count += 1
        return IndexResult(source_id=source_id, indexed=len(chunks))

    def delete_source_paths(self, source_id: str, paths: object) -> int:
        removed = 0
        for path in list(paths):  # type: ignore[call-overload]
            removed += len(self.by_path.pop(path, []))
        return removed

    def reset_calls(self) -> None:
        self.index_calls.clear()
        self.embed_count = 0


class FakeLedger:
    """In-memory ``ResourceLedger`` with seen-tracking + tombstone sweep."""

    def __init__(self) -> None:
        self.rows: dict[tuple[uuid.UUID, str], LedgerRow] = {}
        self.seen: dict[tuple[uuid.UUID, str], uuid.UUID] = {}

    def get(self, source_id: uuid.UUID, resource_uri: str) -> LedgerRow | None:
        return self.rows.get((source_id, resource_uri))

    def upsert(self, row: LedgerRow, *, sync_run_id: uuid.UUID) -> None:
        key = (row.knowledge_source_id, row.resource_uri)
        self.rows[key] = row
        self.seen[key] = sync_run_id

    def mark_seen(self, source_id: uuid.UUID, resource_uri: str, *, sync_run_id: uuid.UUID) -> None:
        self.seen[(source_id, resource_uri)] = sync_run_id

    def tombstone_unseen(self, source_id: uuid.UUID, *, sync_run_id: uuid.UUID) -> list[str]:
        purged: list[str] = []
        for (sid, uri), row in self.rows.items():
            if sid != source_id or row.deleted_at is not None:
                continue
            if self.seen.get((sid, uri)) != sync_run_id:
                row.chunk_count = 0
                row.deleted_at = datetime.now(UTC)
                purged.append(uri)
        return purged

    def purge_source(self, source_id: uuid.UUID) -> int:
        keys = [k for k in self.rows if k[0] == source_id]
        for key in keys:
            del self.rows[key]
        return len(keys)


@dataclass
class FakeRunRecorder:
    """In-memory ``SyncRunRecorder`` capturing closed reports."""

    reports: list[SyncReport] = field(default_factory=list)

    def open(
        self, *, source_id: uuid.UUID, workspace_id: uuid.UUID, mode: SyncDirection
    ) -> uuid.UUID:
        return uuid.uuid4()

    def close(self, report: SyncReport) -> None:
        self.reports.append(report)
