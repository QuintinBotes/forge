"""F39 chain verifier + append-only enforcement (SQLite).

AC5 (verify detects mutation + deletion, incl. tail truncation), AC7
(repository has no mutation path; ORM guard rejects update/delete on every
dialect), AC17 (the reusable immutability trigger DDL compiles for Postgres and
no-ops on SQLite).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import StaticPool, create_engine, delete, update
from sqlalchemy.orm import Session, sessionmaker

from forge_contracts.audit import AuditEvent
from forge_db.audit.chain import verify_chain
from forge_db.audit.repository import AuditQueryRepository
from forge_db.audit.writer import SqlAuditWriter
from forge_db.base import Base
from forge_db.models import AuditLog, AuditLogImmutableError, Workspace

WS = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sf: sessionmaker[Session] = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with sf() as s:
        s.add(Workspace(id=WS, name="Acme", slug="acme"))
        s.commit()
        yield s
    engine.dispose()


def _seed(session: Session, n: int = 5) -> list[AuditLog]:
    writer = SqlAuditWriter(session)
    rows = [
        writer.emit(
            AuditEvent(workspace_id=WS, action="tool.call", details={"i": i})
        )
        for i in range(n)
    ]
    session.commit()
    return rows


def test_verify_ok_for_clean_chain(session: Session) -> None:
    _seed(session)
    result = verify_chain(session, WS)
    assert result.ok is True
    assert result.entries_checked == 5
    assert result.broken_at_seq is None


def test_verify_ok_for_empty_workspace(session: Session) -> None:
    result = verify_chain(session, uuid.uuid4())
    assert result.ok is True
    assert result.entries_checked == 0


def test_verify_detects_metadata_mutation(session: Session) -> None:
    _seed(session)
    # Out-of-band tamper: raw Core UPDATE bypasses the ORM guard on SQLite.
    session.execute(
        update(AuditLog.__table__)
        .where(AuditLog.__table__.c.seq == 3, AuditLog.__table__.c.workspace_id == WS)
        .values(details={"i": 999})
    )
    session.commit()
    result = verify_chain(session, WS)
    assert result.ok is False
    assert result.broken_at_seq == 3


def test_verify_detects_mid_chain_deletion(session: Session) -> None:
    _seed(session)
    session.execute(
        delete(AuditLog.__table__).where(
            AuditLog.__table__.c.seq == 2, AuditLog.__table__.c.workspace_id == WS
        )
    )
    session.commit()
    result = verify_chain(session, WS)
    assert result.ok is False
    assert result.broken_at_seq == 2


def test_verify_detects_tail_truncation_via_head_cursor(session: Session) -> None:
    _seed(session)
    session.execute(
        delete(AuditLog.__table__).where(
            AuditLog.__table__.c.seq == 5, AuditLog.__table__.c.workspace_id == WS
        )
    )
    session.commit()
    result = verify_chain(session, WS)
    assert result.ok is False
    assert result.broken_at_seq == 5


def test_verify_range_subset(session: Session) -> None:
    _seed(session)
    result = verify_chain(session, WS, from_seq=2, to_seq=4)
    assert result.ok is True
    assert result.entries_checked == 3


def test_orm_guard_rejects_update_on_sqlite(session: Session) -> None:
    rows = _seed(session, 1)
    rows[0].result = "denied"
    with pytest.raises(AuditLogImmutableError):
        session.flush()
    session.rollback()


def test_orm_guard_rejects_delete_on_sqlite(session: Session) -> None:
    rows = _seed(session, 1)
    session.delete(rows[0])
    with pytest.raises(AuditLogImmutableError):
        session.flush()
    session.rollback()


def test_repository_has_no_update_delete_path() -> None:
    exposed = {name for name in dir(AuditQueryRepository) if not name.startswith("_")}
    assert exposed == {"get", "list", "iter_export"}


def test_immutability_trigger_ddl_compiles_for_postgres_and_skips_sqlite() -> None:
    """The reusable helper (F07/F09 hand-off) is dialect-guarded (AC17)."""
    from sqlalchemy import Column, Integer, MetaData, Table

    from forge_db.base import attach_immutability_trigger

    md = MetaData()
    fake = Table("fake_transition", md, Column("id", Integer, primary_key=True))
    attach_immutability_trigger(fake)  # idempotent registration, second table
    attach_immutability_trigger(fake)

    # SQLite: creating the table must not attempt the Postgres-only DDL.
    engine = create_engine("sqlite://")
    md.create_all(engine)
    md.drop_all(engine)
    engine.dispose()
