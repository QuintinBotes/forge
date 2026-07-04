"""F39 worker tests: async audit sink + scheduled chain verifier (AC10/AC16).

Hermetic in-memory SQLite. ``audit.record`` persists a serialized event into
the chain; ``audit.verify_chain_all`` returns per-workspace verdicts and, on a
tampered chain, records a ``system``/``critical`` ``audit.chain_broken`` event.
Task + beat registration asserted against the shared Celery app.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import StaticPool, create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker

from forge_contracts.audit import AuditEvent
from forge_db.audit.writer import SqlAuditWriter
from forge_db.base import Base
from forge_db.models import AuditLog, Workspace
from forge_worker.beat import AUDIT_VERIFY_TASK as BEAT_AUDIT_VERIFY_TASK
from forge_worker.beat import BEAT_SCHEDULE
from forge_worker.celery_app import celery_app
from forge_worker.tasks.audit import (
    AUDIT_RECORD_TASK,
    AUDIT_VERIFY_TASK,
    run_record,
    run_verify_all,
)

WS = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
WS2 = uuid.UUID("00000000-0000-0000-0000-0000000000a2")


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
        s.commit()
        yield s
    engine.dispose()


def test_record_task_persists_event_into_chain(session: Session) -> None:
    payload = AuditEvent(
        workspace_id=WS,
        action="tool.call",
        actor_type="agent_runner",
        details={"tool": "read_file", "api_key": "sk-ant-superSecretValue123456"},
    ).model_dump(mode="json")

    run_record(session, payload)

    row = session.scalars(select(AuditLog)).one()
    assert row.seq == 1
    assert row.action == "tool.call"
    assert row.entry_hash is not None
    # The async path redacts too (AC4): the raw secret never persists.
    assert "superSecretValue" not in str(row.details)


def test_verify_all_ok_and_detects_tamper(session: Session) -> None:
    writer = SqlAuditWriter(session)
    for _ in range(3):
        writer.emit(AuditEvent(workspace_id=WS, action="tool.call"))
    writer.emit(AuditEvent(workspace_id=WS2, action="tool.call"))
    session.commit()

    results = run_verify_all(session)
    assert results[str(WS)].ok is True
    assert results[str(WS2)].ok is True

    # Tamper out-of-band (raw Core UPDATE bypasses the ORM guard).
    session.execute(
        update(AuditLog.__table__)
        .where(AuditLog.__table__.c.workspace_id == WS, AuditLog.__table__.c.seq == 2)
        .values(details={"tampered": True})
    )
    session.commit()

    results = run_verify_all(session)
    assert results[str(WS)].ok is False
    assert results[str(WS)].broken_at_seq == 2
    assert results[str(WS2)].ok is True

    # The break itself was audited: system/critical audit.chain_broken (AC16).
    broken_events = session.scalars(
        select(AuditLog).where(
            AuditLog.workspace_id == WS, AuditLog.action == "audit.chain_broken"
        )
    ).all()
    assert len(broken_events) == 1
    event = broken_events[0]
    assert event.actor_type == "system"
    assert event.severity == "critical"
    assert event.details["broken_at_seq"] == 2


def test_tasks_and_beat_are_registered() -> None:
    assert AUDIT_RECORD_TASK in celery_app.tasks
    assert AUDIT_VERIFY_TASK in celery_app.tasks
    assert BEAT_AUDIT_VERIFY_TASK == AUDIT_VERIFY_TASK
    entry = BEAT_SCHEDULE["audit-verify-chain-all"]
    assert entry["task"] == AUDIT_VERIFY_TASK


# --------------------------------------------------------------------------- #
# HARD-11: cover the Celery seams (record_audit_event retry/fail-open,          #
# verify_chain_all) over an injected session factory.                          #
# --------------------------------------------------------------------------- #


def _memory_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory: sessionmaker[Session] = sessionmaker(
        bind=engine, expire_on_commit=False, class_=Session
    )
    with factory() as s:
        s.add(Workspace(id=WS, name="Acme", slug="acme"))
        s.commit()
    return factory


def test_record_audit_event_seam_persists(monkeypatch) -> None:
    import forge_worker.tasks.audit as audit_mod
    from forge_worker.tasks.audit import record_audit_event

    factory = _memory_factory()
    monkeypatch.setattr(audit_mod, "create_session_factory", lambda: factory)

    payload = AuditEvent(workspace_id=WS, action="tool.call").model_dump(mode="json")
    record_audit_event(payload)

    with factory() as s:
        row = s.scalars(select(AuditLog)).one()
        assert row.action == "tool.call"


def test_record_audit_event_drops_after_max_retries(monkeypatch) -> None:
    import forge_worker.tasks.audit as audit_mod
    from forge_worker.tasks.audit import record_audit_event

    factory = _memory_factory()
    monkeypatch.setattr(audit_mod, "create_session_factory", lambda: factory)

    def _boom(_session, _payload):
        raise RuntimeError("db down")

    monkeypatch.setattr(audit_mod, "run_record", _boom)

    def _retry(*_a, **_k):
        raise record_audit_event.MaxRetriesExceededError

    monkeypatch.setattr(record_audit_event, "retry", _retry)
    # Fail-open: exhausting retries logs and returns None (never raises).
    assert record_audit_event({"action": "x"}) is None


def test_verify_chain_all_seam(monkeypatch) -> None:
    import forge_worker.tasks.audit as audit_mod
    from forge_worker.tasks.audit import verify_chain_all

    factory = _memory_factory()
    with factory() as s:
        writer = SqlAuditWriter(s)
        writer.emit(AuditEvent(workspace_id=WS, action="tool.call"))
        s.commit()
    monkeypatch.setattr(audit_mod, "create_session_factory", lambda: factory)

    verdicts = verify_chain_all()
    assert verdicts[str(WS)] is True
