"""Tests for the SQLAlchemy MCP ledger + sync-run recorder (F20).

Hermetic: SQLite (which enforces CHECK constraints), so the tombstone CHECK
(AC8) and tenant isolation (AC15) are exercised without a live Postgres.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from forge_db.base import Base
from forge_db.models import KnowledgeSource, MCPIndexedResource, Workspace
from forge_db.models.enums import RunStatus
from forge_db.session import create_session_factory
from forge_knowledge.mcp_indexer import LedgerRow, SyncDirection, SyncReport
from forge_knowledge.mcp_ledger import (
    SqlResourceLedger,
    SqlSyncRunRecorder,
    index_status_counts,
    latest_run,
    purge_index,
)


@pytest.fixture
def factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _seed_source(factory: sessionmaker[Session], *, ws: uuid.UUID | None = None) -> uuid.UUID:
    ws = ws or uuid.uuid4()
    with factory() as session:
        if session.get(Workspace, ws) is None:
            session.add(Workspace(id=ws, name=f"ws-{ws.hex[:6]}", slug=f"ws-{ws.hex[:6]}"))
            session.flush()
        source = KnowledgeSource(
            workspace_id=ws,
            kind="mcp",
            name="confluence-engineering",
            uri="mcp://confluence-engineering",
        )
        session.add(source)
        session.flush()
        sid = source.id
        session.commit()
    return sid


def _row(source_id: uuid.UUID, uri: str, *, token: str | None = "v1", chunks: int = 2) -> LedgerRow:
    return LedgerRow(
        knowledge_source_id=source_id,
        resource_uri=uri,
        connection_slug="confluence-engineering",
        change_token=token,
        content_hash=f"hash-{uri}",
        chunk_count=chunks,
        byte_size=10,
        last_indexed_at=datetime.now(UTC),
    )


def test_upsert_get_and_mark_seen(factory: sessionmaker[Session]) -> None:
    sid = _seed_source(factory)
    ledger = SqlResourceLedger(factory)
    run = uuid.uuid4()

    assert ledger.get(sid, "confluence://engineering/p1") is None
    ledger.upsert(_row(sid, "confluence://engineering/p1"), sync_run_id=run)

    got = ledger.get(sid, "confluence://engineering/p1")
    assert got is not None
    assert got.chunk_count == 2
    assert got.change_token == "v1"

    # Idempotent upsert updates in place (no duplicate row).
    ledger.upsert(_row(sid, "confluence://engineering/p1", token="v2", chunks=3), sync_run_id=run)
    again = ledger.get(sid, "confluence://engineering/p1")
    assert again is not None and again.change_token == "v2" and again.chunk_count == 3
    res_count, chunk_count = index_status_counts(factory, sid)
    assert res_count == 1 and chunk_count == 3


def test_tombstone_unseen_marks_and_returns_uris(factory: sessionmaker[Session]) -> None:
    sid = _seed_source(factory)
    ledger = SqlResourceLedger(factory)
    run1 = uuid.uuid4()
    ledger.upsert(_row(sid, "uri-a"), sync_run_id=run1)
    ledger.upsert(_row(sid, "uri-b"), sync_run_id=run1)

    # Next run only re-sees uri-a; uri-b should be tombstoned.
    run2 = uuid.uuid4()
    ledger.mark_seen(sid, "uri-a", sync_run_id=run2)
    purged = ledger.tombstone_unseen(sid, sync_run_id=run2)

    assert purged == ["uri-b"]
    tombstoned = ledger.get(sid, "uri-b")
    assert tombstoned is not None and tombstoned.deleted_at is not None
    assert tombstoned.chunk_count == 0
    # Counts exclude the tombstoned resource.
    res_count, _ = index_status_counts(factory, sid)
    assert res_count == 1


def test_check_constraint_blocks_tombstone_with_chunks(factory: sessionmaker[Session]) -> None:
    sid = _seed_source(factory)
    with factory() as session:
        ws = session.get(KnowledgeSource, sid).workspace_id  # type: ignore[union-attr]
        session.add(
            MCPIndexedResource(
                workspace_id=ws,
                knowledge_source_id=sid,
                connection_slug="confluence-engineering",
                resource_uri="bad",
                content_hash="h",
                chunk_count=5,
                deleted_at=datetime.now(UTC),
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_run_recorder_open_and_close(factory: sessionmaker[Session]) -> None:
    sid = _seed_source(factory)
    with factory() as session:
        ws = session.get(KnowledgeSource, sid).workspace_id  # type: ignore[union-attr]
    recorder = SqlSyncRunRecorder(factory)
    run_id = recorder.open(source_id=sid, workspace_id=ws, mode=SyncDirection.full)

    report = SyncReport(
        knowledge_source_id=sid,
        sync_run_id=run_id,
        mode=SyncDirection.full,
        resources_seen=3,
        resources_indexed=3,
        chunks_indexed=6,
        finished_at=datetime.now(UTC),
    )
    recorder.close(report)

    run = latest_run(factory, sid)
    assert run is not None
    assert run.status is RunStatus.SUCCEEDED
    assert run.resources_seen == 3 and run.chunks_indexed == 6


def test_failed_run_records_error(factory: sessionmaker[Session]) -> None:
    sid = _seed_source(factory)
    with factory() as session:
        ws = session.get(KnowledgeSource, sid).workspace_id  # type: ignore[union-attr]
    recorder = SqlSyncRunRecorder(factory)
    run_id = recorder.open(source_id=sid, workspace_id=ws, mode=SyncDirection.incremental)
    recorder.close(
        SyncReport(
            knowledge_source_id=sid,
            sync_run_id=run_id,
            mode=SyncDirection.incremental,
            error="boom",
        )
    )
    run = latest_run(factory, sid)
    assert run is not None and run.status is RunStatus.FAILED and run.error == "boom"


def test_purge_index_removes_only_target_source(factory: sessionmaker[Session]) -> None:
    ws = uuid.uuid4()
    sid_a = _seed_source(factory, ws=ws)
    sid_b = _seed_source(factory, ws=ws)
    ledger = SqlResourceLedger(factory)
    run = uuid.uuid4()
    ledger.upsert(_row(sid_a, "a1"), sync_run_id=run)
    ledger.upsert(_row(sid_b, "b1"), sync_run_id=run)

    removed = purge_index(factory, sid_a)
    assert removed == 1
    assert index_status_counts(factory, sid_a) == (0, 0)
    assert index_status_counts(factory, sid_b)[0] == 1


def test_tenant_isolation_counts(factory: sessionmaker[Session]) -> None:
    sid_a = _seed_source(factory)
    sid_b = _seed_source(factory)
    ledger = SqlResourceLedger(factory)
    run = uuid.uuid4()
    # Identical resource uri indexed under two different sources/workspaces.
    ledger.upsert(_row(sid_a, "confluence://engineering/shared"), sync_run_id=run)
    ledger.upsert(_row(sid_b, "confluence://engineering/shared"), sync_run_id=run)

    assert index_status_counts(factory, sid_a)[0] == 1
    assert index_status_counts(factory, sid_b)[0] == 1
    assert ledger.get(sid_a, "confluence://engineering/shared") is not None
    assert ledger.get(sid_b, "confluence://engineering/shared") is not None
