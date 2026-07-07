"""F39 writer unit tests (SQLite): chain assignment, redaction, fail-open.

AC2 (monotonic per-workspace chain), AC3 (deterministic hashes), AC4 (secrets
redacted before hashing/persistence), AC9 (caller-session atomicity), AC10
(fail-open async path).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from forge_contracts.audit import (
    GENESIS_HASH,
    AuditEvent,
    compute_entry_hash,
    compute_payload_hash,
)
from forge_db.audit.writer import SqlAuditWriter
from forge_db.base import Base
from forge_db.models import AuditChainHead, AuditLog, Workspace

WS = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
WS2 = uuid.UUID("00000000-0000-0000-0000-0000000000a2")


class FakeRedactor:
    """Deterministic marker redactor (string leaves only)."""

    def redact(self, text: str) -> str:
        return text.replace("SECRET", "[REDACTED]")

    def redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {k: self.redact_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.redact_value(v) for v in value]
        return value


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


def _event(ws: uuid.UUID = WS, **overrides: Any) -> AuditEvent:
    kwargs: dict[str, Any] = {
        "workspace_id": ws,
        "action": "tool.call",
        "actor_type": "agent_runner",
        "actor_label": "agent_run:abc",
        "details": {"tool": "read_file"},
    }
    kwargs.update(overrides)
    return AuditEvent(**kwargs)


def test_emit_assigns_monotonic_seq_and_links_chain(session: Session) -> None:
    writer = SqlAuditWriter(session)
    r1 = writer.emit(_event())
    r2 = writer.emit(_event(action="agent.action"))
    r3 = writer.emit(_event(action="approval.decided"))
    session.commit()

    assert [r1.seq, r2.seq, r3.seq] == [1, 2, 3]
    assert r1.prev_hash == GENESIS_HASH
    assert r2.prev_hash == r1.entry_hash
    assert r3.prev_hash == r2.entry_hash

    head = session.scalars(select(AuditChainHead).where(AuditChainHead.workspace_id == WS)).one()
    assert head.last_seq == 3
    assert head.last_hash == r3.entry_hash


def test_two_workspaces_keep_independent_chains(session: Session) -> None:
    writer = SqlAuditWriter(session)
    a1 = writer.emit(_event(WS))
    b1 = writer.emit(_event(WS2))
    a2 = writer.emit(_event(WS))
    session.commit()

    assert (a1.seq, a2.seq) == (1, 2)
    assert b1.seq == 1
    assert b1.prev_hash == GENESIS_HASH
    assert a2.prev_hash == a1.entry_hash


def test_stored_hashes_recompute_identically(session: Session) -> None:
    writer = SqlAuditWriter(session)
    row = writer.emit(_event(details={"k": "v"}, before={"a": 1}, after={"a": 2}))
    session.commit()

    payload = compute_payload_hash(
        {"before": row.before, "after": row.after, "details": row.details}
    )
    assert row.payload_hash == payload
    assert row.entry_hash == compute_entry_hash(
        prev_hash=row.prev_hash or "",
        workspace_id=row.workspace_id,
        seq=row.seq or 0,
        occurred_at=row.created_at,
        actor_type=row.actor_type,
        actor_id=row.actor_id,
        actor_label=row.actor_label,
        action=row.action,
        target_type=row.target_type,
        target_id=row.target_id,
        scope_type=row.scope_type,
        scope_id=row.scope_id,
        result=row.result,
        payload_hash=payload,
    )


def test_emit_redacts_metadata_before_hash_and_store(session: Session) -> None:
    writer = SqlAuditWriter(session, redactor=FakeRedactor())
    row = writer.emit(
        _event(
            details={"nested": {"token": "SECRET-abc"}, "list": ["ok", "SECRET"]},
            before={"key": "SECRET"},
            reason="because SECRET said so",
        )
    )
    session.commit()

    assert row.details == {"nested": {"token": "[REDACTED]-abc"}, "list": ["ok", "[REDACTED]"]}
    assert row.before == {"key": "[REDACTED]"}
    assert row.reason == "because [REDACTED] said so"
    # The hash covers the REDACTED payload — the secret never entered the chain.
    assert row.payload_hash == compute_payload_hash(
        {"before": row.before, "after": row.after, "details": row.details}
    )
    stored = session.get(AuditLog, row.id)
    assert stored is not None
    assert "SECRET" not in str(stored.details)


def test_emit_async_dispatches_and_never_raises(session: Session) -> None:
    dispatched: list[dict[str, Any]] = []
    writer = SqlAuditWriter(session, async_dispatch=dispatched.append)
    writer.emit_async(_event())
    assert len(dispatched) == 1
    assert dispatched[0]["action"] == "tool.call"

    def _boom(_payload: dict[str, Any]) -> None:
        raise RuntimeError("broker down")

    failing = SqlAuditWriter(session, async_dispatch=_boom)
    failing.emit_async(_event(action="agent.action"))  # must not raise (AC10)
    session.commit()
    # Fallback sync emit persisted the event despite the dispatcher failure.
    actions = {r.action for r in session.scalars(select(AuditLog)).all()}
    assert "agent.action" in actions


def test_critical_emit_rolls_back_with_caller_transaction(session: Session) -> None:
    writer = SqlAuditWriter(session)
    writer.emit(_event(severity="critical", action="approval.decided"))
    session.rollback()  # the surrounding action aborts -> audit row aborts too
    assert session.scalars(select(AuditLog)).all() == []
    assert session.scalars(select(AuditChainHead)).all() == []
