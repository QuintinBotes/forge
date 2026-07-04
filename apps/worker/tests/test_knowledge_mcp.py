"""Integration tests for the F20 worker MCP sync tasks (Postgres-backed).

Exercises the real pipeline end-to-end: the F09 gateway adapter
(``GatewayMcpResourceFetcher`` over ``MCPGatewayClient`` + ``FakeTransport`` — no
live MCP traffic) -> ``McpSyncIndexer`` -> pgvector ``retrieval_chunk`` store +
the ``mcp_indexed_resource`` ledger + ``knowledge_sync_run`` rows.

Covers AC3 (persist chunks/ledger/run), AC4 (namespace scoping), AC6 (incremental
no-op by hash), AC8 (tombstone), AC9 (index-served retrieval, zero live calls),
AC10 (provenance), AC11 (redaction at rest), AC12 (freshness beat), AC17 (read-only).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from forge_contracts import MCPResource
from forge_contracts.dtos import KnowledgeScope
from forge_db.base import Base
from forge_db.models import (
    KnowledgeSource,
    KnowledgeSyncRun,
    MCPIndexedResource,
    RetrievalChunk,
    Workspace,
)
from forge_db.models.enums import KnowledgeSourceKind, RunStatus, SyncMode
from forge_knowledge import (
    DeterministicEmbeddingClient,
    FixtureRerankerClient,
    KnowledgeService,
    SyncDirection,
)
from forge_mcp.client import MCPGatewayClient
from forge_mcp.testing import FakeTransport, sample_connection, sample_transport
from forge_worker.adapters.gateway_fetcher import GatewayMcpResourceFetcher
from forge_worker.tasks.knowledge_mcp import refresh_stale_mcp_sources, run_sync

pytestmark = pytest.mark.usefixtures("pg_engine")

SLUG = "confluence-engineering"


@pytest.fixture
def pg_factory(pg_engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    try:
        yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(pg_engine)


def _seed_source(
    factory: sessionmaker[Session],
    *,
    namespaces: tuple[str, ...] = ("engineering", "architecture"),
    status: str = "pending",
    last_synced_at: datetime | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    with factory() as session:
        ws = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
        session.add(ws)
        session.flush()
        source = KnowledgeSource(
            workspace_id=ws.id,
            kind=KnowledgeSourceKind.MCP,
            name=SLUG,
            uri=f"mcp://{SLUG}",
            sync_mode=SyncMode.SYNC_AND_INDEX,
            freshness_sla_minutes=30,
            last_synced_at=last_synced_at,
            config={
                "mcp_connection_id": SLUG,
                "allowed_namespaces": list(namespaces),
                "index_strategy": "sync_and_index",
                "index_status": status,
            },
        )
        session.add(source)
        session.flush()
        ids = (ws.id, source.id)
        session.commit()
    return ids


def _store(factory: sessionmaker[Session]) -> KnowledgeService:
    # Default embedding dimension matches the pgvector Vector(1536) column.
    return KnowledgeService.from_session_factory(
        factory, DeterministicEmbeddingClient(), FixtureRerankerClient()
    )


def _fetcher(transport: FakeTransport | None = None) -> GatewayMcpResourceFetcher:
    client = MCPGatewayClient(transport=transport or sample_transport())
    client.connect(sample_connection())
    return GatewayMcpResourceFetcher(client, connection_slug=SLUG)


def test_full_sync_persists_chunks_ledger_and_run(pg_factory: sessionmaker[Session]) -> None:
    _, src_id = _seed_source(pg_factory)
    report = run_sync(
        str(src_id),
        SyncDirection.full,
        session_factory=pg_factory,
        store=_store(pg_factory),
        fetcher=_fetcher(),
    )

    # AC4: finance is outside allowed_namespaces -> never enumerated (3, not 4).
    assert report.resources_seen == 3
    assert report.resources_indexed == 3
    assert report.error is None

    with pg_factory() as session:
        chunks = list(
            session.scalars(
                select(RetrievalChunk).where(RetrievalChunk.knowledge_source_id == src_id)
            )
        )
        assert chunks
        assert all(c.chunk_type.value == "mcp_resource" for c in chunks)
        assert all(c.weight == 1.0 for c in chunks)
        # AC10: provenance path mcp://{slug}/{uri} + metadata.
        assert all(c.path.startswith(f"mcp://{SLUG}/") for c in chunks)
        assert all(c.chunk_metadata.get("resource_uri") for c in chunks)
        assert all(
            c.chunk_metadata.get("source_uri", "").startswith(f"mcp://{SLUG}/") for c in chunks
        )
        # AC4: no finance resource leaked into the index.
        assert all("finance" not in c.path for c in chunks)
        # AC11: the fixture secret is redacted at rest.
        assert all("sk-fixture-secret-123" not in c.content for c in chunks)

        ledger = list(
            session.scalars(
                select(MCPIndexedResource).where(MCPIndexedResource.knowledge_source_id == src_id)
            )
        )
        assert len(ledger) == 3
        assert all(r.chunk_count > 0 and r.deleted_at is None for r in ledger)

        run = session.scalar(
            select(KnowledgeSyncRun).where(KnowledgeSyncRun.knowledge_source_id == src_id)
        )
        assert run is not None
        assert run.status is RunStatus.SUCCEEDED
        assert run.resources_seen == 3

        source = session.get(KnowledgeSource, src_id)
        assert source is not None
        assert source.config["index_status"] == "ready"
        assert source.last_synced_at is not None


def test_retrieval_is_index_served_with_zero_live_calls(
    pg_factory: sessionmaker[Session],
) -> None:
    ws_id, src_id = _seed_source(pg_factory)
    transport = sample_transport()
    run_sync(
        str(src_id),
        SyncDirection.full,
        session_factory=pg_factory,
        store=_store(pg_factory),
        fetcher=_fetcher(transport),
    )

    # Reset transport interaction state, then search the local index.
    transport.calls.clear()
    store = _store(pg_factory)
    results = store.search("rotate the vault token", KnowledgeScope(workspace_id=ws_id), k=5)

    assert results  # served from the persisted index
    assert any(c.chunk_type.value == "mcp_resource" for c in results)
    # AC9: no live MCP tool call happened during retrieval.
    assert transport.calls == []


def test_incremental_no_change_is_noop(pg_factory: sessionmaker[Session]) -> None:
    _, src_id = _seed_source(pg_factory)
    store = _store(pg_factory)
    run_sync(
        str(src_id), SyncDirection.full, session_factory=pg_factory, store=store, fetcher=_fetcher()
    )

    report = run_sync(
        str(src_id),
        SyncDirection.incremental,
        session_factory=pg_factory,
        store=store,
        fetcher=_fetcher(),
    )

    # No change-token from the fixture -> content-hash fallback skips all three.
    assert report.resources_skipped == 3
    assert report.resources_indexed == 0


def test_tombstone_removes_upstream_deleted_resource(pg_factory: sessionmaker[Session]) -> None:
    _, src_id = _seed_source(pg_factory, namespaces=("engineering",))
    store = _store(pg_factory)

    two = FakeTransport(
        resources=[
            MCPResource(uri="confluence://engineering/a", name="A", namespace="engineering"),
            MCPResource(uri="confluence://engineering/b", name="B", namespace="engineering"),
        ],
        contents={
            "confluence://engineering/a": "# A\n\nAlpha content.",
            "confluence://engineering/b": "# B\n\nBeta content.",
        },
    )
    run_sync(
        str(src_id),
        SyncDirection.full,
        session_factory=pg_factory,
        store=store,
        fetcher=_fetcher(two),
    )

    one = FakeTransport(
        resources=[
            MCPResource(uri="confluence://engineering/a", name="A", namespace="engineering")
        ],
        contents={"confluence://engineering/a": "# A\n\nAlpha content."},
    )
    report = run_sync(
        str(src_id),
        SyncDirection.incremental,
        session_factory=pg_factory,
        store=store,
        fetcher=_fetcher(one),
    )

    assert report.resources_deleted == 1
    with pg_factory() as session:
        rows = {
            r.resource_uri: r
            for r in session.scalars(
                select(MCPIndexedResource).where(MCPIndexedResource.knowledge_source_id == src_id)
            )
        }
        assert rows["confluence://engineering/b"].deleted_at is not None
        assert rows["confluence://engineering/b"].chunk_count == 0
        remaining = list(
            session.scalars(
                select(RetrievalChunk).where(RetrievalChunk.knowledge_source_id == src_id)
            )
        )
        assert all("/b" not in c.path for c in remaining)


def test_read_only_no_tool_calls_ever(pg_factory: sessionmaker[Session]) -> None:
    _, src_id = _seed_source(pg_factory)
    transport = sample_transport()
    store = _store(pg_factory)
    fetcher = _fetcher(transport)
    run_sync(
        str(src_id), SyncDirection.full, session_factory=pg_factory, store=store, fetcher=fetcher
    )
    run_sync(
        str(src_id),
        SyncDirection.incremental,
        session_factory=pg_factory,
        store=store,
        fetcher=fetcher,
    )
    # AC17: only resources/list + resources/read were used; no tool was invoked.
    assert transport.calls == []


# --------------------------------------------------------------------------- #
# Freshness beat (AC12)                                                       #
# --------------------------------------------------------------------------- #


def test_refresh_enqueues_only_stale_ready_sources(pg_factory: sessionmaker[Session]) -> None:
    now = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)
    _, stale_id = _seed_source(pg_factory, status="ready", last_synced_at=now - timedelta(hours=2))
    _, fresh_id = _seed_source(
        pg_factory, status="ready", last_synced_at=now - timedelta(minutes=1)
    )
    enqueued: list[str] = []
    result = refresh_stale_mcp_sources(session_factory=pg_factory, enqueue=enqueued.append, now=now)

    assert str(stale_id) in enqueued
    assert str(fresh_id) not in enqueued
    assert result["enqueued"] == enqueued


def test_refresh_ignores_non_ready_sources(pg_factory: sessionmaker[Session]) -> None:
    now = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)
    _, pending_id = _seed_source(
        pg_factory, status="pending", last_synced_at=now - timedelta(hours=5)
    )
    enqueued: list[str] = []
    refresh_stale_mcp_sources(session_factory=pg_factory, enqueue=enqueued.append, now=now)
    assert str(pending_id) not in enqueued


def test_refresh_isolates_per_source_enqueue_failure(pg_factory: sessionmaker[Session]) -> None:
    now = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)
    _seed_source(pg_factory, status="ready", last_synced_at=now - timedelta(hours=2))

    def boom(_source_id: str) -> None:
        raise RuntimeError("broker down")

    # Must not raise: an isolated enqueue failure never aborts the batch.
    result = refresh_stale_mcp_sources(session_factory=pg_factory, enqueue=boom, now=now)
    assert result["enqueued"] == []
