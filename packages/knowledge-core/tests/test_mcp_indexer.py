"""Unit tests for the F20 MCP sync-and-index pipeline (pure, no DB/network).

Covers AC3 (full sync), AC5 (skip by change-token), AC6 (skip by content-hash),
AC7 (re-index only the changed resource), AC8 (tombstone + sweep-skip on partial
enumeration), AC16 (max-resources cap), AC17 (only list/read ever called).
"""

from __future__ import annotations

import uuid
from typing import NamedTuple

import pytest
from mcp_fakes import (
    SLUG,
    FakeChunkStore,
    FakeLedger,
    FakeMcpResourceFetcher,
    FakeResource,
    FakeRunRecorder,
)

from forge_knowledge.mcp_chunking import McpResourceChunker, provenance_uri
from forge_knowledge.mcp_indexer import McpSyncIndexer, SyncDirection

SOURCE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


class Harness(NamedTuple):
    indexer: McpSyncIndexer
    store: FakeChunkStore
    ledger: FakeLedger
    runs: FakeRunRecorder


def _indexer(fetcher: FakeMcpResourceFetcher, **kwargs: object) -> Harness:
    store = FakeChunkStore()
    ledger = FakeLedger()
    runs = FakeRunRecorder()
    indexer = McpSyncIndexer(
        fetcher=fetcher,
        chunker=McpResourceChunker(),
        indexer=store,
        ledger=ledger,
        runs=runs,
        **kwargs,  # type: ignore[arg-type]
    )
    return Harness(indexer, store, ledger, runs)


def _sync(harness: Harness, mode: SyncDirection):
    return harness.indexer.sync(
        source_id=SOURCE_ID,
        workspace_id=WORKSPACE_ID,
        connection_slug=SLUG,
        allowed_namespaces=["engineering", "architecture"],
        mode=mode,
    )


def _resources() -> list[FakeResource]:
    return [
        FakeResource(uri="confluence://engineering/page-1", body="# Runbook\n\nRotate the vault."),
        FakeResource(uri="confluence://engineering/page-2", body="# Services\n\nGeneral page."),
        FakeResource(
            uri="confluence://architecture/adr-7",
            body="# ADR 7\n\nDecision record.",
            namespace="architecture",
        ),
    ]


def test_full_sync_indexes_all_text_resources() -> None:
    h = _indexer(FakeMcpResourceFetcher(_resources()))

    report = _sync(h, SyncDirection.full)

    assert report.resources_seen == 3
    assert report.resources_indexed == 3
    assert report.chunks_indexed > 0
    # One non-deleted ledger row per resource, each with chunks.
    assert len(h.ledger.rows) == 3
    assert all(r.chunk_count > 0 and r.deleted_at is None for r in h.ledger.rows.values())
    # Provenance: every chunk path is mcp://{slug}/{uri}.
    for res in _resources():
        path = provenance_uri(SLUG, res.uri)
        assert h.store.by_path[path]
        chunk = h.store.by_path[path][0]
        assert chunk.chunk_type.value == "mcp_resource"
        assert chunk.weight == 1.0
        assert chunk.metadata["resource_uri"] == res.uri
        assert chunk.metadata["connection_slug"] == SLUG
        assert chunk.metadata["source_uri"] == path
    assert h.runs.reports[-1].sync_run_id == report.sync_run_id


def test_incremental_skips_unchanged_by_change_token() -> None:
    resources = [
        FakeResource(uri="confluence://engineering/page-1", body="body one", change_token="v1"),
        FakeResource(uri="confluence://engineering/page-2", body="body two", change_token="v1"),
    ]
    fetcher = FakeMcpResourceFetcher(resources)
    h = _indexer(fetcher)
    _sync(h, SyncDirection.full)

    fetcher.read_counts.clear()
    h.store.reset_calls()
    report = _sync(h, SyncDirection.incremental)

    # No body was read and nothing was embedded for unchanged resources.
    assert sum(fetcher.read_counts.values()) == 0
    assert h.store.index_calls == []
    assert report.resources_skipped == 2
    assert report.resources_indexed == 0


def test_incremental_skips_unchanged_by_content_hash_when_no_token() -> None:
    resources = [
        FakeResource(uri="confluence://engineering/page-1", body="stable body", change_token=None),
    ]
    fetcher = FakeMcpResourceFetcher(resources)
    h = _indexer(fetcher)
    _sync(h, SyncDirection.full)

    fetcher.read_counts.clear()
    h.store.reset_calls()
    report = _sync(h, SyncDirection.incremental)

    # Body is read (no token), but identical hash -> no re-chunk / re-embed.
    assert fetcher.read_counts["confluence://engineering/page-1"] == 1
    assert h.store.index_calls == []
    assert h.store.embed_count == 0
    assert report.resources_skipped == 1


def test_change_reindexes_only_changed_resource() -> None:
    fetcher = FakeMcpResourceFetcher(
        [
            FakeResource(
                uri="confluence://engineering/page-1", body="original one", change_token="v1"
            ),
            FakeResource(
                uri="confluence://engineering/page-2", body="original two", change_token="v1"
            ),
        ]
    )
    h = _indexer(fetcher)
    _sync(h, SyncDirection.full)
    before = h.ledger.rows[(SOURCE_ID, "confluence://engineering/page-2")].last_indexed_at

    fetcher.read_counts.clear()
    h.store.reset_calls()
    fetcher.mutate("confluence://engineering/page-1", body="EDITED one", change_token="v2")
    report = _sync(h, SyncDirection.incremental)

    assert report.resources_indexed == 1
    assert report.resources_skipped == 1
    # Only page-1 was read + indexed; page-2 untouched.
    assert fetcher.read_counts["confluence://engineering/page-1"] == 1
    assert fetcher.read_counts["confluence://engineering/page-2"] == 0
    path1 = provenance_uri(SLUG, "confluence://engineering/page-1")
    assert "EDITED one" in h.store.by_path[path1][0].content
    # page-2's last_indexed_at did not advance.
    assert h.ledger.rows[(SOURCE_ID, "confluence://engineering/page-2")].last_indexed_at == before


def test_tombstone_removes_resource_chunks() -> None:
    fetcher = FakeMcpResourceFetcher(_resources())
    h = _indexer(fetcher)
    _sync(h, SyncDirection.full)

    fetcher.remove("confluence://architecture/adr-7")
    report = _sync(h, SyncDirection.incremental)

    assert report.resources_deleted == 1
    assert report.chunks_deleted >= 1
    tombstoned = h.ledger.rows[(SOURCE_ID, "confluence://architecture/adr-7")]
    assert tombstoned.deleted_at is not None
    assert tombstoned.chunk_count == 0
    assert provenance_uri(SLUG, "confluence://architecture/adr-7") not in h.store.by_path


def test_sweep_skipped_on_partial_enumeration() -> None:
    h = _indexer(FakeMcpResourceFetcher(_resources()))
    _sync(h, SyncDirection.full)

    # New fetcher that paginates and aborts mid-cursor; only page 1 is seen.
    partial = FakeMcpResourceFetcher(_resources(), page_size=2, fail_mid_enumeration=True)
    indexer2 = McpSyncIndexer(
        fetcher=partial,
        chunker=McpResourceChunker(),
        indexer=h.store,
        ledger=h.ledger,
        runs=h.runs,
    )
    report = indexer2.sync(
        source_id=SOURCE_ID,
        workspace_id=WORKSPACE_ID,
        connection_slug=SLUG,
        allowed_namespaces=None,
        mode=SyncDirection.incremental,
    )

    assert report.sweep_skipped is True
    assert report.resources_deleted == 0
    # No resource was tombstoned despite the incomplete enumeration.
    assert all(row.deleted_at is None for row in h.ledger.rows.values())


def test_max_resources_cap_enforced() -> None:
    resources = [
        FakeResource(uri=f"confluence://engineering/page-{i}", body=f"body {i}") for i in range(5)
    ]
    fetcher = FakeMcpResourceFetcher(resources, page_size=2)
    h = _indexer(fetcher, max_resources=2)

    report = _sync(h, SyncDirection.full)

    assert report.resources_seen == 2
    assert report.cap_hit is True
    assert report.sweep_skipped is True  # incomplete enumeration -> never sweeps


def test_only_list_and_read_are_ever_called() -> None:
    """AC17: no mutating tool path exists — only list/read across full+incremental."""
    fetcher = FakeMcpResourceFetcher(_resources())
    h = _indexer(fetcher)
    _sync(h, SyncDirection.full)
    _sync(h, SyncDirection.incremental)

    # The fetcher protocol only exposes list_resources / read_resource; the test
    # asserts those are the only interactions (no call_tool attribute is touched).
    assert not hasattr(fetcher, "call_tool")
    assert fetcher.list_calls >= 2


def test_binary_resource_skipped_with_zero_chunks() -> None:
    fetcher = FakeMcpResourceFetcher(
        [FakeResource(uri="confluence://engineering/img", body="\x00binary", mime_type="image/png")]
    )
    h = _indexer(fetcher)
    report = _sync(h, SyncDirection.full)

    row = h.ledger.rows[(SOURCE_ID, "confluence://engineering/img")]
    assert row.chunk_count == 0
    assert report.chunks_indexed == 0
    assert h.store.by_path == {}


@pytest.mark.parametrize("mode", [SyncDirection.full, SyncDirection.incremental])
def test_sync_run_closed_with_report(mode: SyncDirection) -> None:
    h = _indexer(FakeMcpResourceFetcher(_resources()))
    report = _sync(h, mode)
    assert h.runs.reports[-1] is report
    assert report.finished_at is not None
    assert report.error is None
