"""F39 Postgres integration: DB-level immutability + concurrent chain safety.

AC6 (BEFORE UPDATE/DELETE trigger raises on ``audit_log``) and AC8 (concurrent
``emit`` calls serialized by the ``audit_chain_head ... FOR UPDATE`` lock keep
the chain gap-free). Parks without Postgres (shared ``pg_engine`` fixture).
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from forge_contracts.audit import AuditEvent
from forge_db.audit.chain import verify_chain
from forge_db.audit.writer import SqlAuditWriter
from forge_db.base import Base
from forge_db.models import AuditLog, Workspace

pytestmark = pytest.mark.usefixtures("pg_engine")


@pytest.fixture
def factory(pg_engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    try:
        yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(pg_engine)


@pytest.fixture
def workspace_id(factory) -> uuid.UUID:
    ws_id = uuid.uuid4()
    with factory() as s:
        s.add(Workspace(id=ws_id, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}"))
        s.commit()
    return ws_id


def _emit(session: Session, ws: uuid.UUID, action: str = "tool.call") -> None:
    SqlAuditWriter(session).emit(AuditEvent(workspace_id=ws, action=action))


def test_trigger_blocks_update_and_delete(factory, workspace_id) -> None:
    with factory() as s:
        _emit(s, workspace_id)
        s.commit()

    with factory() as s:
        with pytest.raises(DBAPIError):
            s.execute(
                update(AuditLog.__table__)
                .where(AuditLog.__table__.c.workspace_id == workspace_id)
                .values(result="denied")
            )
        s.rollback()
        with pytest.raises(DBAPIError):
            s.execute(
                delete(AuditLog.__table__).where(AuditLog.__table__.c.workspace_id == workspace_id)
            )
        s.rollback()


def test_concurrent_emit_keeps_linear_chain(factory, workspace_id) -> None:
    # Genesis head first (avoids a benign insert race in the very first append).
    with factory() as s:
        _emit(s, workspace_id, action="agent.action")
        s.commit()

    errors: list[Exception] = []

    def worker(n: int) -> None:
        try:
            for _ in range(n):
                with factory() as s:
                    _emit(s, workspace_id)
                    s.commit()
        except Exception as exc:  # pragma: no cover - failure reporting
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(10,)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []

    with factory() as s:
        seqs = sorted(
            s.scalars(select(AuditLog.seq).where(AuditLog.workspace_id == workspace_id)).all()
        )
        assert seqs == list(range(1, 42))  # 1 seed + 40 concurrent, gap-free
        result = verify_chain(s, workspace_id)
        assert result.ok is True
        assert result.entries_checked == 41
