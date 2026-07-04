"""F39 query repository: filters + keyset pagination + isolation (AC11/AC12)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

from forge_contracts.audit import AuditEvent
from forge_db.audit.repository import AuditQueryRepository, decode_cursor, encode_cursor
from forge_db.audit.writer import SqlAuditWriter
from forge_db.base import Base
from forge_db.models import Workspace

WS = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
WS2 = uuid.UUID("00000000-0000-0000-0000-0000000000a2")
ACTOR = uuid.uuid4()
NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sf: sessionmaker[Session] = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with sf() as s:
        s.add(Workspace(id=WS, name="Acme", slug="acme"))
        s.add(Workspace(id=WS2, name="Rival", slug="rival"))
        writer = SqlAuditWriter(s)
        for i in range(10):
            writer.emit(
                AuditEvent(
                    workspace_id=WS,
                    action="tool.call" if i % 2 == 0 else "approval.decided",
                    actor_type="agent_runner" if i % 2 == 0 else "user",
                    actor_id=None if i % 2 == 0 else ACTOR,
                    result="success" if i < 8 else "denied",
                    severity="info" if i < 9 else "critical",
                    details={"marker": f"needle-{i}"},
                    created_at=NOW + timedelta(minutes=i),
                )
            )
        writer.emit(AuditEvent(workspace_id=WS2, action="tool.call"))
        s.commit()
        yield s
    engine.dispose()


def test_list_is_workspace_scoped_newest_first(session: Session) -> None:
    repo = AuditQueryRepository(session)
    rows, _ = repo.list(WS, limit=100)
    assert len(rows) == 10
    assert [r.seq for r in rows] == list(range(10, 0, -1))
    rows2, _ = repo.list(WS2, limit=100)
    assert len(rows2) == 1


def test_filters(session: Session) -> None:
    repo = AuditQueryRepository(session)
    assert len(repo.list(WS, action=["tool.call"])[0]) == 5
    assert len(repo.list(WS, actor_type="user")[0]) == 5
    assert len(repo.list(WS, actor_id=ACTOR)[0]) == 5
    assert len(repo.list(WS, result="denied")[0]) == 2
    assert len(repo.list(WS, severity="critical")[0]) == 1
    assert len(repo.list(WS, q="needle-7")[0]) == 1
    window = repo.list(
        WS, from_time=NOW + timedelta(minutes=2), to_time=NOW + timedelta(minutes=4)
    )[0]
    assert [r.seq for r in window] == [5, 4, 3]


def test_keyset_pagination_is_gapless_and_terminates(session: Session) -> None:
    repo = AuditQueryRepository(session)
    seen: list[int] = []
    cursor: str | None = None
    pages = 0
    while True:
        rows, cursor = repo.list(WS, limit=3, cursor=cursor)
        seen.extend(r.seq or 0 for r in rows)
        pages += 1
        if cursor is None:
            break
    assert seen == list(range(10, 0, -1))  # no gaps, no duplicates
    assert pages == 4


def test_cursor_roundtrip_and_garbage_is_ignored() -> None:
    assert decode_cursor(encode_cursor(42)) == 42
    assert decode_cursor("!!not-base64!!") is None
    assert decode_cursor(encode_cursor(1)[:-2] + "zz") in (None, 1)


def test_get_is_workspace_isolated(session: Session) -> None:
    repo = AuditQueryRepository(session)
    rows, _ = repo.list(WS, limit=1)
    entry = rows[0]
    assert repo.get(WS, entry.id) is not None
    assert repo.get(WS2, entry.id) is None  # foreign workspace -> not found


def test_iter_export_streams_chain_order(session: Session) -> None:
    repo = AuditQueryRepository(session)
    seqs = [r.seq for r in repo.iter_export(WS, batch_size=4)]
    assert seqs == list(range(1, 11))
