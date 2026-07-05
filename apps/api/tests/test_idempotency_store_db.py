"""Postgres integration tests for :class:`DbIdempotencyStore` (idempotency-store persistence).

Exercises the DB-backed HTTP idempotency store against a real pgvector Postgres
via the shared ``pg_engine`` fixture (root ``conftest.py``): the
``IdempotencyStore`` protocol end-to-end — byte-exact round-trip of every
``StoredResponse`` field, the ``put_if_absent`` reserve semantics (first write
wins, a live key is a no-op, an *expired* key is overwritten), read-time expiry,
durability across independent store instances, the ``key`` UNIQUE constraint, the
``purge_expired`` sweep, a full ``IdempotencyMiddleware`` round-trip (retry
replays, side effect runs once) driven through the DB store, and structural
conformance to the same frozen protocol the in-memory store implements. Skips
cleanly (parked) when no Postgres is reachable; runs under
``FORGE_TEST_DATABASE_URL`` (pgvector :5433) in the gate.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from forge_api.middleware.idempotency import (
    IdempotencyMiddleware,
    IdempotencyStore,
    InMemoryIdempotencyStore,
    StoredResponse,
)
from forge_api.middleware.idempotency_db import DbIdempotencyStore
from forge_db.base import Base
from forge_db.models import IdempotencyKey

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("pg_engine")]


@pytest.fixture
def factory(pg_engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    try:
        yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(pg_engine)


@pytest.fixture
def store(factory: sessionmaker[Session]) -> DbIdempotencyStore:
    return DbIdempotencyStore(factory)


def _response(**kwargs: object) -> StoredResponse:
    fields: dict[str, object] = {
        "request_hash": "reqhash-0001",
        "status_code": 200,
        "body": b'{"ok":true}',
        "content_type": "application/json",
        "created_at": datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
    }
    fields.update(kwargs)
    return StoredResponse(**fields)  # type: ignore[arg-type]


def _insert_expired(factory: sessionmaker[Session], key: str, value: StoredResponse) -> None:
    """Insert a row whose TTL is already in the past (a stale cache entry)."""
    import base64

    with factory() as session:
        session.add(
            IdempotencyKey(
                key=key,
                response={
                    "request_hash": value.request_hash,
                    "status_code": value.status_code,
                    "content_type": value.content_type,
                    "body_b64": base64.b64encode(value.body).decode("ascii"),
                },
                created_at=value.created_at,
                expires_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        session.commit()


# --------------------------------------------------------------------------- #
# Protocol conformance                                                        #
# --------------------------------------------------------------------------- #


def test_db_store_satisfies_idempotency_store_protocol(store: DbIdempotencyStore) -> None:
    assert isinstance(store, IdempotencyStore)


# --------------------------------------------------------------------------- #
# Round-trip                                                                  #
# --------------------------------------------------------------------------- #


def test_round_trip_preserves_all_fields(store: DbIdempotencyStore) -> None:
    created = datetime(2026, 6, 7, 8, 9, 10, 123456, tzinfo=UTC)
    value = _response(
        request_hash="abc123",
        status_code=201,
        body=b'{"id":42,"name":"x"}',
        content_type="application/json; charset=utf-8",
        created_at=created,
    )
    assert store.put_if_absent("k-round", value, 3600) is True

    loaded = store.get("k-round")
    assert loaded is not None
    assert loaded.request_hash == "abc123"
    assert loaded.status_code == 201
    assert loaded.body == b'{"id":42,"name":"x"}'
    assert loaded.content_type == "application/json; charset=utf-8"
    assert loaded.created_at == created


def test_round_trip_preserves_non_utf8_body(store: DbIdempotencyStore) -> None:
    """The body is opaque bytes (base64 in JSONB): arbitrary bytes round-trip exactly."""
    raw = bytes(range(256))  # includes non-UTF-8 bytes
    assert store.put_if_absent("k-bin", _response(body=raw), 3600) is True
    loaded = store.get("k-bin")
    assert loaded is not None
    assert loaded.body == raw


def test_get_missing_returns_none(store: DbIdempotencyStore) -> None:
    assert store.get("nope") is None


# --------------------------------------------------------------------------- #
# put_if_absent reserve semantics                                             #
# --------------------------------------------------------------------------- #


def test_put_if_absent_first_write_wins(store: DbIdempotencyStore) -> None:
    first = _response(request_hash="first", body=b"one")
    second = _response(request_hash="second", body=b"two")
    assert store.put_if_absent("k", first, 3600) is True
    assert store.put_if_absent("k", second, 3600) is False  # live entry: no-op

    loaded = store.get("k")
    assert loaded is not None
    assert loaded.request_hash == "first"  # original preserved, not overwritten
    assert loaded.body == b"one"


def test_put_if_absent_overwrites_expired_entry(
    store: DbIdempotencyStore, factory: sessionmaker[Session]
) -> None:
    stale = _response(request_hash="stale", body=b"old")
    _insert_expired(factory, "k-exp", stale)

    fresh = _response(request_hash="fresh", body=b"new")
    assert store.put_if_absent("k-exp", fresh, 3600) is True  # expired ⇒ overwrite

    loaded = store.get("k-exp")
    assert loaded is not None
    assert loaded.request_hash == "fresh"
    assert loaded.body == b"new"


# --------------------------------------------------------------------------- #
# Expiry                                                                       #
# --------------------------------------------------------------------------- #


def test_get_treats_expired_as_absent(
    store: DbIdempotencyStore, factory: sessionmaker[Session]
) -> None:
    _insert_expired(factory, "k-old", _response())
    assert store.get("k-old") is None


def test_purge_expired_removes_only_stale_rows(
    store: DbIdempotencyStore, factory: sessionmaker[Session]
) -> None:
    _insert_expired(factory, "stale-1", _response())
    _insert_expired(factory, "stale-2", _response())
    assert store.put_if_absent("live", _response(), 3600) is True

    assert store.purge_expired() == 2
    with factory() as session:
        remaining = {row.key for row in session.query(IdempotencyKey).all()}
    assert remaining == {"live"}


# --------------------------------------------------------------------------- #
# Constraints + durability                                                    #
# --------------------------------------------------------------------------- #


def test_key_unique_constraint_enforced(factory: sessionmaker[Session]) -> None:
    """The ``key`` UNIQUE index (the ON CONFLICT target) is schema-enforced."""
    row = IdempotencyKey(
        key="dupe",
        response={"request_hash": "h", "status_code": 200, "content_type": "x", "body_b64": ""},
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    dupe = IdempotencyKey(
        key="dupe",
        response={"request_hash": "h", "status_code": 200, "content_type": "x", "body_b64": ""},
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    with pytest.raises(IntegrityError), factory() as session:
        session.add_all([row, dupe])
        session.commit()


def test_persists_across_store_instances(factory: sessionmaker[Session]) -> None:
    value = _response(request_hash="durable", body=b"kept")
    assert DbIdempotencyStore(factory).put_if_absent("k-dur", value, 3600) is True

    reloaded = DbIdempotencyStore(factory).get("k-dur")
    assert reloaded is not None
    assert reloaded.request_hash == "durable"
    assert reloaded.body == b"kept"


# --------------------------------------------------------------------------- #
# Full middleware round-trip through the DB store                             #
# --------------------------------------------------------------------------- #


def test_middleware_replays_through_db_store(factory: sessionmaker[Session]) -> None:
    counter = {"n": 0}
    app = FastAPI()

    @app.post("/work")
    async def work(request: Request) -> dict[str, int]:
        counter["n"] += 1
        body = await request.json()
        return {"run": counter["n"], "echo": body.get("v", 0)}

    app.add_middleware(IdempotencyMiddleware, store=DbIdempotencyStore(factory), ttl_s=3600)
    client = TestClient(app)
    headers = {"Idempotency-Key": uuid.uuid4().hex}

    first = client.post("/work", json={"v": 1}, headers=headers)
    second = client.post("/work", json={"v": 1}, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()  # identical replayed body
    assert counter["n"] == 1  # side effect ran exactly once (durable dedup)
    assert second.headers.get("idempotency-replayed") == "true"

    # A different body under the same key is a client bug → 422, still no re-run.
    mismatch = client.post("/work", json={"v": 999}, headers=headers)
    assert mismatch.status_code == 422
    assert counter["n"] == 1


# --------------------------------------------------------------------------- #
# Parity with the in-memory store (same protocol, identical behaviour)        #
# --------------------------------------------------------------------------- #


def test_matches_in_memory_store_behaviour(store: DbIdempotencyStore) -> None:
    mem = InMemoryIdempotencyStore()
    value = _response(request_hash="parity", body=b"same")

    assert store.put_if_absent("k", value, 3600) == mem.put_if_absent("k", value, 3600)
    assert store.put_if_absent("k", value, 3600) == mem.put_if_absent("k", value, 3600)
    assert (store.get("k") is None) == (mem.get("k") is None)
    assert (store.get("missing") is None) == (mem.get("missing") is None)

    db_hit = store.get("k")
    mem_hit = mem.get("k")
    assert db_hit is not None and mem_hit is not None
    assert db_hit.request_hash == mem_hit.request_hash
    assert db_hit.body == mem_hit.body
