"""Postgres integration tests for :class:`DbAPIKeyBackend`.

Exercises the DB-backed platform API-key backend against a real pgvector Postgres
via the shared ``pg_engine`` fixture (root ``conftest.py``): the ``add`` /
``by_prefix`` / ``list`` / ``get`` seam end-to-end — full :class:`APIKeyRecord`
round-trip, workspace-scoped filtering + ordering, prefix indexing, the
``platform_api_key`` referential-integrity boundary, overwrite-on-re-add, and the
enum-taxonomy bridge — plus the full :class:`APIKeyStore` behaviours that flow
through the backend (mint → verify last-used stamp, revoke, revoke-for-user,
list) proving byte-for-byte parity with the in-memory store.

Skips cleanly (parked) when no Postgres is reachable; runs under
``FORGE_TEST_DATABASE_URL`` (pgvector :5433) in the gate.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from forge_api.auth.apikeys import (
    APIKeyBackend,
    APIKeyRecord,
    APIKeyStore,
    InMemoryAPIKeyBackend,
    _token_prefix,
    generate_api_token,
)
from forge_api.auth.apikeys_db import DbAPIKeyBackend
from forge_contracts.auth import PlatformKeyKind
from forge_contracts.enums import APIKeyKind, UserRole
from forge_db.base import Base
from forge_db.models import PlatformAPIKey, User, Workspace

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("pg_engine")]

SECRET = b"unit-test-apikey-subkey-0123456789"


@pytest.fixture
def factory(pg_engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    try:
        yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(pg_engine)


@pytest.fixture
def ws(factory: sessionmaker[Session]) -> uuid.UUID:
    """A persisted workspace (the ``platform_api_key.workspace_id`` FK target)."""
    workspace_id = uuid.uuid4()
    with factory() as session:
        session.add(Workspace(id=workspace_id, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}"))
        session.commit()
    return workspace_id


@pytest.fixture
def other_ws(factory: sessionmaker[Session]) -> uuid.UUID:
    workspace_id = uuid.uuid4()
    with factory() as session:
        session.add(Workspace(id=workspace_id, name="Beta", slug=f"beta-{uuid.uuid4().hex[:8]}"))
        session.commit()
    return workspace_id


@pytest.fixture
def backend(factory: sessionmaker[Session]) -> DbAPIKeyBackend:
    return DbAPIKeyBackend(factory)


def _record(
    ws: uuid.UUID,
    *,
    kind: APIKeyKind = APIKeyKind.SYSTEM,
    role: UserRole = UserRole.MEMBER,
    token: str | None = None,
    user_id: uuid.UUID | None = None,
    created_at: datetime | None = None,
    last_used_at: datetime | None = None,
    expires_at: datetime | None = None,
    is_active: bool = True,
    record_id: uuid.UUID | None = None,
) -> APIKeyRecord:
    tok = token or generate_api_token(kind)
    return APIKeyRecord(
        id=record_id or uuid.uuid4(),
        workspace_id=ws,
        name="ci",
        kind=kind,
        role=role,
        key_prefix=_token_prefix(tok),
        token_hash=f"hash-{uuid.uuid4().hex}",
        user_id=user_id,
        created_at=created_at or datetime.now(UTC),
        last_used_at=last_used_at,
        expires_at=expires_at,
        is_active=is_active,
    )


# --------------------------------------------------------------------------- #
# Protocol conformance                                                        #
# --------------------------------------------------------------------------- #


def test_backend_satisfies_apikey_backend_protocol(backend: DbAPIKeyBackend) -> None:
    assert isinstance(backend, APIKeyBackend)


# --------------------------------------------------------------------------- #
# add + round-trip                                                            #
# --------------------------------------------------------------------------- #


def test_add_round_trips_every_field(backend: DbAPIKeyBackend, ws: uuid.UUID) -> None:
    created = datetime.now(UTC) - timedelta(minutes=5)
    used = datetime.now(UTC) - timedelta(minutes=1)
    expires = datetime.now(UTC) + timedelta(hours=1)
    record = _record(
        ws,
        role=UserRole.ADMIN,
        created_at=created,
        last_used_at=used,
        expires_at=expires,
    )
    backend.add(record)

    got = backend.get(ws, record.id)
    assert got is not None
    assert got.id == record.id
    assert got.workspace_id == ws
    assert got.name == record.name
    assert got.kind == APIKeyKind.SYSTEM  # SYSTEM ⇄ service round-trips verbatim
    assert got.role == UserRole.ADMIN
    assert got.key_prefix == record.key_prefix
    assert got.token_hash == record.token_hash
    assert got.user_id is None
    assert got.is_active is True
    assert got.created_at == created
    assert got.last_used_at == used
    assert got.expires_at == expires
    for value in (got.created_at, got.last_used_at, got.expires_at):
        assert value.tzinfo is not None


def test_kind_bridge_persists_platform_kind(
    backend: DbAPIKeyBackend, ws: uuid.UUID, factory: sessionmaker[Session]
) -> None:
    backend.add(_record(ws, kind=APIKeyKind.SYSTEM))
    with factory() as session:
        row = session.scalars(select(PlatformAPIKey)).one()
    # Stored under the frozen column's taxonomy...
    assert row.kind is PlatformKeyKind.SERVICE
    # ...and read back as the record's own kind.
    assert backend.list(ws)[0].kind == APIKeyKind.SYSTEM


def test_add_persists_user_id_as_created_by(
    backend: DbAPIKeyBackend, ws: uuid.UUID, factory: sessionmaker[Session]
) -> None:
    user_id = uuid.uuid4()
    with factory() as session:
        session.add(
            User(id=user_id, workspace_id=ws, email=f"u-{user_id.hex[:6]}@acme.dev", name="U")
        )
        session.commit()
    backend.add(_record(ws, user_id=user_id))
    got = backend.list(ws)[0]
    assert got.user_id == user_id


def test_add_overwrites_on_repeated_id(backend: DbAPIKeyBackend, ws: uuid.UUID) -> None:
    """Re-adding the same id overwrites (mirrors the dict store's ``add``)."""
    rid = uuid.uuid4()
    backend.add(_record(ws, role=UserRole.MEMBER, record_id=rid))
    backend.add(_record(ws, role=UserRole.ADMIN, record_id=rid))
    keys = backend.list(ws)
    assert len(keys) == 1
    assert keys[0].role == UserRole.ADMIN


# --------------------------------------------------------------------------- #
# referential-integrity boundary                                              #
# --------------------------------------------------------------------------- #


def test_add_unknown_workspace_rejected(backend: DbAPIKeyBackend) -> None:
    with pytest.raises(IntegrityError):
        backend.add(_record(uuid.uuid4()))  # workspace FK has no target row


# --------------------------------------------------------------------------- #
# by_prefix                                                                   #
# --------------------------------------------------------------------------- #


def test_by_prefix_returns_only_matching(backend: DbAPIKeyBackend, ws: uuid.UUID) -> None:
    tok_a = generate_api_token(APIKeyKind.SYSTEM)  # prefix "forge_sy"
    tok_b = generate_api_token(APIKeyKind.MCP_TOKEN)  # distinct prefix "forge_mc"
    a = _record(ws, token=tok_a)
    backend.add(a)
    backend.add(_record(ws, kind=APIKeyKind.MCP_TOKEN, token=tok_b))
    matches = backend.by_prefix(_token_prefix(tok_a))
    assert [m.id for m in matches] == [a.id]
    assert matches[0].key_prefix == _token_prefix(tok_a)


def test_by_prefix_returns_all_sharing_a_prefix(backend: DbAPIKeyBackend, ws: uuid.UUID) -> None:
    # Every SYSTEM token shares the display prefix "forge_sy" — the exact case the
    # store's constant-time verify fans out over.
    records = [_record(ws) for _ in range(3)]
    prefix = records[0].key_prefix
    assert all(r.key_prefix == prefix for r in records)
    for rec in records:
        backend.add(rec)
    matches = backend.by_prefix(prefix)
    assert {m.id for m in matches} == {r.id for r in records}


def test_by_prefix_unknown_is_empty(backend: DbAPIKeyBackend, ws: uuid.UUID) -> None:
    backend.add(_record(ws))
    assert backend.by_prefix("forge_zz") == []


# --------------------------------------------------------------------------- #
# list: workspace scoping + ordering                                          #
# --------------------------------------------------------------------------- #


def test_list_is_workspace_scoped(
    backend: DbAPIKeyBackend, ws: uuid.UUID, other_ws: uuid.UUID
) -> None:
    backend.add(_record(ws))
    backend.add(_record(ws))
    backend.add(_record(other_ws))
    assert len(backend.list(ws)) == 2
    assert len(backend.list(other_ws)) == 1
    assert backend.list(uuid.uuid4()) == []


def test_list_orders_oldest_first(backend: DbAPIKeyBackend, ws: uuid.UUID) -> None:
    base = datetime.now(UTC) - timedelta(hours=1)
    first = _record(ws, created_at=base)
    second = _record(ws, created_at=base + timedelta(minutes=1))
    third = _record(ws, created_at=base + timedelta(minutes=2))
    for rec in (third, first, second):  # insert out of order
        backend.add(rec)
    assert [r.id for r in backend.list(ws)] == [first.id, second.id, third.id]


# --------------------------------------------------------------------------- #
# get: workspace isolation                                                    #
# --------------------------------------------------------------------------- #


def test_get_respects_workspace(
    backend: DbAPIKeyBackend, ws: uuid.UUID, other_ws: uuid.UUID
) -> None:
    record = _record(ws)
    backend.add(record)
    assert backend.get(ws, record.id) is not None
    assert backend.get(other_ws, record.id) is None  # right id, wrong tenant
    assert backend.get(ws, uuid.uuid4()) is None  # unknown id


# --------------------------------------------------------------------------- #
# durability                                                                  #
# --------------------------------------------------------------------------- #


def test_persists_across_backend_instances(factory: sessionmaker[Session], ws: uuid.UUID) -> None:
    record = _record(ws)
    DbAPIKeyBackend(factory).add(record)
    assert DbAPIKeyBackend(factory).get(ws, record.id) is not None


# --------------------------------------------------------------------------- #
# APIKeyStore end-to-end parity (mutation flows through the backend)          #
# --------------------------------------------------------------------------- #


def test_store_mint_verify_stamps_last_used(factory: sessionmaker[Session], ws: uuid.UUID) -> None:
    store = APIKeyStore(secret_key=SECRET, backend=DbAPIKeyBackend(factory))
    info, token = store.mint(workspace_id=ws, name="ci", role=UserRole.MEMBER)

    verified = store.verify(token)
    assert verified is not None
    assert verified.id == info.id
    # verify() stamps last_used_at by mutating the returned record; the DB backend
    # write-throughs that so it is durable (not just on the transient object).
    reread = DbAPIKeyBackend(factory).get(ws, info.id)
    assert reread is not None and reread.last_used_at is not None


def test_store_verify_rejects_unknown_token(factory: sessionmaker[Session], ws: uuid.UUID) -> None:
    store = APIKeyStore(secret_key=SECRET, backend=DbAPIKeyBackend(factory))
    store.mint(workspace_id=ws, name="ci", role=UserRole.MEMBER)
    assert store.verify("forge_sy_not-a-real-token") is None


def test_store_verify_rejects_expired(factory: sessionmaker[Session], ws: uuid.UUID) -> None:
    store = APIKeyStore(secret_key=SECRET, backend=DbAPIKeyBackend(factory))
    _, token = store.mint(
        workspace_id=ws,
        name="ci",
        role=UserRole.MEMBER,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert store.verify(token) is None


def test_store_revoke_persists(factory: sessionmaker[Session], ws: uuid.UUID) -> None:
    store = APIKeyStore(secret_key=SECRET, backend=DbAPIKeyBackend(factory))
    info, token = store.mint(workspace_id=ws, name="ci", role=UserRole.MEMBER)
    assert store.verify(token) is not None

    assert store.revoke(ws, info.id) is True
    # Revocation flows through the write-through record → row.revoked_at set.
    assert store.verify(token) is None
    reread = DbAPIKeyBackend(factory).get(ws, info.id)
    assert reread is not None and reread.is_active is False
    # Idempotent-ish: revoking an unknown key is False.
    assert store.revoke(ws, uuid.uuid4()) is False


def test_store_revoke_for_user_persists(factory: sessionmaker[Session], ws: uuid.UUID) -> None:
    user_id = uuid.uuid4()
    with factory() as session:
        session.add(
            User(id=user_id, workspace_id=ws, email=f"u-{user_id.hex[:6]}@acme.dev", name="U")
        )
        session.commit()
    store = APIKeyStore(secret_key=SECRET, backend=DbAPIKeyBackend(factory))
    _, t1 = store.mint(workspace_id=ws, name="a", role=UserRole.MEMBER, user_id=user_id)
    _, t2 = store.mint(workspace_id=ws, name="b", role=UserRole.MEMBER, user_id=user_id)
    _, other = store.mint(workspace_id=ws, name="c", role=UserRole.MEMBER)

    assert store.revoke_for_user(ws, user_id) == 2
    assert store.verify(t1) is None
    assert store.verify(t2) is None
    assert store.verify(other) is not None  # untouched


def test_store_list_keys_reflects_state(factory: sessionmaker[Session], ws: uuid.UUID) -> None:
    store = APIKeyStore(secret_key=SECRET, backend=DbAPIKeyBackend(factory))
    info, _ = store.mint(workspace_id=ws, name="ci", role=UserRole.MEMBER)
    keys = store.list_keys(ws)
    assert [k.id for k in keys] == [info.id]
    assert keys[0].is_active is True

    store.revoke(ws, info.id)
    assert store.list_keys(ws)[0].is_active is False


# --------------------------------------------------------------------------- #
# parity with the in-memory backend (same seam, identical observable results) #
# --------------------------------------------------------------------------- #


def test_matches_in_memory_backend_behaviour(factory: sessionmaker[Session], ws: uuid.UUID) -> None:
    db = DbAPIKeyBackend(factory)
    mem = InMemoryAPIKeyBackend()

    record = _record(ws, role=UserRole.VIEWER)
    db.add(record)
    mem.add(record)

    db_got = db.get(ws, record.id)
    mem_got = mem.get(ws, record.id)
    assert db_got is not None and mem_got is not None
    assert (db_got.id, db_got.role, db_got.kind, db_got.key_prefix) == (
        mem_got.id,
        mem_got.role,
        mem_got.kind,
        mem_got.key_prefix,
    )
    assert [r.id for r in db.by_prefix(record.key_prefix)] == [
        r.id for r in mem.by_prefix(record.key_prefix)
    ]
    assert [r.id for r in db.list(ws)] == [r.id for r in mem.list(ws)]
    assert db.get(uuid.uuid4(), record.id) is mem.get(uuid.uuid4(), record.id)  # None
