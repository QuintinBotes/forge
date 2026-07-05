"""Postgres integration tests for :class:`DbAuditStore` (audit-store persistence).

Exercises the DB-backed observability audit store against a real pgvector
Postgres via the shared ``pg_engine`` fixture (root ``conftest.py``): the
``AuditStore`` protocol end-to-end — append + hash chaining, full round-trip via
``all()``, every ``query`` filter (category / actor / run_id / connection_id /
workspace_id), chain ordering, the ``limit`` edge cases, ``verify_integrity`` on
a clean chain, out-of-band tamper detection, the unique-``seq`` constraint,
durability + a continued global chain across independent store instances, and
structural conformance to the same frozen ``AuditStore`` protocol the in-memory
store implements. Skips cleanly (parked) when no Postgres is reachable; runs
under ``FORGE_TEST_DATABASE_URL`` (pgvector :5433) in the gate.

Each behaviour mirrors ``tests/test_obs_audit.py`` (the in-memory contract), so
both backends are proven to satisfy the same protocol identically.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from forge_api.observability.audit import (
    AuditCategory,
    AuditEntry,
    AuditLog,
    AuditStore,
    InMemoryAuditStore,
    verify_chain,
)
from forge_api.observability.audit_db import DbAuditStore
from forge_db.base import Base
from forge_db.models.observability_audit import ObservabilityAuditEntry

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("pg_engine")]


@pytest.fixture
def factory(pg_engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    try:
        yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(pg_engine)


@pytest.fixture
def store(factory: sessionmaker[Session]) -> DbAuditStore:
    return DbAuditStore(factory)


def _entry(action: str, **kwargs: object) -> AuditEntry:
    return AuditEntry(category=AuditCategory.AGENT_ACTION, action=action, **kwargs)


# --------------------------------------------------------------------------- #
# Protocol conformance                                                        #
# --------------------------------------------------------------------------- #


def test_db_store_satisfies_audit_store_protocol(store: DbAuditStore) -> None:
    assert isinstance(store, AuditStore)


# --------------------------------------------------------------------------- #
# Append + hash chain                                                         #
# --------------------------------------------------------------------------- #


def test_append_assigns_global_monotonic_sequence(store: DbAuditStore) -> None:
    a = store.append(_entry("plan"))
    b = store.append(_entry("write"))
    c = store.append(_entry("approve"))
    assert [a.seq, b.seq, c.seq] == [0, 1, 2]


def test_entries_are_hash_chained_to_predecessor(store: DbAuditStore) -> None:
    first = store.append(_entry("a0"))
    second = store.append(_entry("a1"))
    assert first.entry_hash
    assert second.prev_hash == first.entry_hash


def test_round_trip_preserves_all_fields_and_rehashes(store: DbAuditStore) -> None:
    run = uuid.uuid4()
    ws = uuid.uuid4()
    appended = store.append(
        AuditEntry(
            category=AuditCategory.MCP_CALL,
            action="search_docs",
            actor="agent-runner",
            workspace_id=ws,
            run_id=run,
            target="search_docs",
            connection_id="conn-1",
            status="ok",
            detail="looked up docs",
            payload_hash="abc123",
            latency_ms=42,
            metadata={"endpoint": "/v1/things"},
        )
    )
    (loaded,) = store.all()
    assert loaded == appended  # frozen model equality: every field round-trips
    assert loaded.workspace_id == ws
    assert loaded.run_id == run
    assert loaded.connection_id == "conn-1"
    assert loaded.latency_ms == 42
    assert loaded.metadata == {"endpoint": "/v1/things"}
    # The persisted hash re-verifies against the reconstructed entry.
    assert verify_chain(store.all()) is True


def test_workspace_id_is_optional(store: DbAuditStore) -> None:
    entry = store.append(_entry("boot", workspace_id=None))
    assert entry.workspace_id is None
    assert store.all()[0].workspace_id is None


# --------------------------------------------------------------------------- #
# Query: filtering, ordering, limit                                           #
# --------------------------------------------------------------------------- #


def test_query_filters_by_category_and_run_id(store: DbAuditStore) -> None:
    run = uuid.uuid4()
    store.append(AuditEntry(category=AuditCategory.AGENT_ACTION, action="plan", run_id=run))
    store.append(AuditEntry(category=AuditCategory.TOOL_CALL, action="write", run_id=run))
    store.append(AuditEntry(category=AuditCategory.TOOL_CALL, action="write", run_id=uuid.uuid4()))

    assert len(store.query(run_id=run)) == 2
    assert len(store.query(category=AuditCategory.TOOL_CALL)) == 2
    assert len(store.query(category=AuditCategory.TOOL_CALL, run_id=run)) == 1


def test_query_filters_by_actor_connection_and_workspace(store: DbAuditStore) -> None:
    ws = uuid.uuid4()
    store.append(_entry("a", actor="alice", workspace_id=ws))
    store.append(_entry("b", actor="bob", connection_id="conn-9"))
    store.append(_entry("c", actor="alice"))

    assert [e.action for e in store.query(actor="alice")] == ["a", "c"]
    assert [e.action for e in store.query(connection_id="conn-9")] == ["b"]
    assert [e.action for e in store.query(workspace_id=ws)] == ["a"]


def test_query_returns_chain_order(store: DbAuditStore) -> None:
    for i in range(5):
        store.append(_entry(f"step-{i}"))
    assert [e.action for e in store.query()] == [f"step-{i}" for i in range(5)]
    assert [e.seq for e in store.all()] == [0, 1, 2, 3, 4]


def test_query_limit_returns_most_recent(store: DbAuditStore) -> None:
    for i in range(5):
        store.append(_entry(f"step-{i}"))
    recent = store.query(limit=2)
    assert [e.action for e in recent] == ["step-3", "step-4"]


def test_query_limit_zero_is_empty_and_negative_is_ignored(store: DbAuditStore) -> None:
    for i in range(3):
        store.append(_entry(f"s{i}"))
    assert store.query(limit=0) == []
    # Negative limit mirrors the in-memory store: the slice is skipped -> all rows.
    assert len(store.query(limit=-1)) == 3


# --------------------------------------------------------------------------- #
# Integrity + tamper detection                                                #
# --------------------------------------------------------------------------- #


def test_clean_chain_verifies(store: DbAuditStore) -> None:
    for i in range(4):
        store.append(_entry(f"a{i}"))
    assert store.verify_integrity() is True


def test_out_of_band_tampering_breaks_verification(
    store: DbAuditStore, factory: sessionmaker[Session]
) -> None:
    for i in range(3):
        store.append(_entry(f"a{i}"))
    assert store.verify_integrity() is True

    # Mutate a persisted row's content out-of-band (the repository exposes no
    # such path); the hash chain must detect it.
    with factory() as session:
        session.execute(
            update(ObservabilityAuditEntry)
            .where(ObservabilityAuditEntry.seq == 1)
            .values(action="MALICIOUS")
        )
        session.commit()

    assert store.verify_integrity() is False


# --------------------------------------------------------------------------- #
# Constraints + durability                                                    #
# --------------------------------------------------------------------------- #


def test_duplicate_seq_is_rejected(store: DbAuditStore, factory: sessionmaker[Session]) -> None:
    store.append(_entry("only"))  # seq 0
    with factory() as session:  # noqa: SIM117 - explicit raises block
        with pytest.raises(IntegrityError):
            session.add(
                ObservabilityAuditEntry(
                    entry_id=uuid.uuid4(),
                    seq=0,  # collides with the existing chain position
                    occurred_at=store.all()[0].timestamp,
                    category="agent_action",
                    action="dupe",
                    status="ok",
                    prev_hash="0" * 64,
                    entry_hash="1" * 64,
                )
            )
            session.commit()


def test_chain_persists_and_continues_across_store_instances(
    factory: sessionmaker[Session],
) -> None:
    first = DbAuditStore(factory)
    first.append(_entry("a0"))
    a1 = first.append(_entry("a1"))

    # A brand-new instance sees the durable trail and continues the same chain.
    second = DbAuditStore(factory)
    assert [e.seq for e in second.all()] == [0, 1]
    appended = second.append(_entry("a2"))
    assert appended.seq == 2
    assert appended.prev_hash == a1.entry_hash
    assert second.verify_integrity() is True


# --------------------------------------------------------------------------- #
# Parity with the in-memory store (same protocol, identical behaviour)        #
# --------------------------------------------------------------------------- #


def test_matches_in_memory_store_behaviour(store: DbAuditStore) -> None:
    mem = InMemoryAuditStore()
    run = uuid.uuid4()
    payloads = [
        _entry("plan", run_id=run),
        AuditEntry(category=AuditCategory.TOOL_CALL, action="write", run_id=run, actor="x"),
        AuditEntry(category=AuditCategory.APPROVAL, action="approve", actor="x"),
    ]
    for payload in payloads:
        # Same input entry into both stores.
        store.append(payload.model_copy())
        mem.append(payload.model_copy())

    assert [e.seq for e in store.all()] == [e.seq for e in mem.all()]
    assert len(store.query(run_id=run)) == len(mem.query(run_id=run))
    assert len(store.query(actor="x")) == len(mem.query(actor="x"))
    assert [e.action for e in store.query(limit=2)] == [e.action for e in mem.query(limit=2)]
    assert store.verify_integrity() is mem.verify_integrity() is True


def test_audit_log_facade_persists_through_db_store(store: DbAuditStore) -> None:
    """The redacting :class:`AuditLog` facade writes through the durable store."""
    log = AuditLog(store)
    entry = log.record(
        category=AuditCategory.TOOL_CALL,
        action="call_api",
        detail="used Authorization: Bearer abcDEF123456ghiJKL",
        metadata={"api_key": "sk-SECRET1234567890", "endpoint": "/v1/things"},
    )
    assert entry.redacted is True
    (persisted,) = store.all()
    assert persisted == entry
    assert "sk-SECRET1234567890" not in persisted.model_dump_json()
    assert "abcDEF123456ghiJKL" not in (persisted.detail or "")
    assert log.verify_integrity() is True
