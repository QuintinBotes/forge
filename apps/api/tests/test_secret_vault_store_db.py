"""Postgres integration tests for :class:`DbSecretStore` (secret-vault persistence).

Exercises the DB-backed encrypted secret store against a real pgvector Postgres
via the shared ``pg_engine`` fixture (root ``conftest.py``): the ``SecretStore``
protocol end-to-end — round-trip of every :class:`StoredSecret` field, upsert
(rotation) semantics, per-workspace isolation (cross-tenant ``get``/``list``/
``remove`` return nothing), oldest-first ordering, ``expires_at`` fidelity driving
the vault's read-time ``SecretExpiredError``, ``SecretNotFoundError`` for a missing
id, the workspace foreign-key constraint, durability across independent store
instances, ``all_records`` for cross-workspace rotation, and structural conformance
to the same frozen protocol the in-memory store implements. Skips cleanly (parked)
when no Postgres is reachable; runs under ``FORGE_TEST_DATABASE_URL`` (pgvector
:5433) in the gate.

Security: the plaintext is never persisted and is never emitted by this module —
assertions compare booleans / ciphertext-absence, never printing a credential.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from forge_api.auth.crypto import HmacAeadCipher, generate_key
from forge_api.auth.vault import (
    SecretExpiredError,
    SecretNotFoundError,
    SecretStore,
    SecretVault,
    StoredSecret,
)
from forge_api.auth.vault_db import DbSecretStore
from forge_contracts.enums import APIKeyKind
from forge_db.base import Base
from forge_db.models import Secret, Workspace

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("pg_engine")]

# A non-secret ciphertext sentinel (this is opaque bytes, not a credential).
_CIPHERTEXT = b"\x02enveloped-opaque-blob-\x00\x01\x02"


@pytest.fixture
def factory(pg_engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    try:
        yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(pg_engine)


@pytest.fixture
def make_workspace(factory: sessionmaker[Session]) -> Callable[[], uuid.UUID]:
    """Insert a real workspace (the ``secret.workspace_id`` FK target) and return its id."""

    def _make() -> uuid.UUID:
        ws = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:12]}")
        with factory() as session:
            session.add(ws)
            session.commit()
            return ws.id

    return _make


@pytest.fixture
def store(factory: sessionmaker[Session]) -> DbSecretStore:
    return DbSecretStore(factory)


def _record(workspace_id: uuid.UUID, **kwargs: object) -> StoredSecret:
    fields: dict[str, object] = {
        "id": uuid.uuid4(),
        "workspace_id": workspace_id,
        "name": "anthropic",
        "kind": APIKeyKind.MODEL_PROVIDER,
        "ciphertext": _CIPHERTEXT,
    }
    fields.update(kwargs)
    return StoredSecret(**fields)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Protocol conformance                                                        #
# --------------------------------------------------------------------------- #


def test_db_store_satisfies_secret_store_protocol(store: DbSecretStore) -> None:
    assert isinstance(store, SecretStore)


# --------------------------------------------------------------------------- #
# Round-trip                                                                  #
# --------------------------------------------------------------------------- #


def test_round_trip_preserves_all_fields(
    store: DbSecretStore, make_workspace: Callable[[], uuid.UUID]
) -> None:
    ws = make_workspace()
    created = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    expires = created + timedelta(days=30)
    used = created + timedelta(hours=1)
    rotated = created + timedelta(days=2)
    record = _record(
        ws,
        name="openai-prod",
        kind=APIKeyKind.INTEGRATION_TOKEN,
        provider="openai",
        key_prefix="sk-o…",
        created_at=created,
        updated_at=created,
        last_used_at=used,
        expires_at=expires,
        key_version=3,
        rotated_at=rotated,
    )
    store.add(record)

    loaded = store.get(ws, record.id)
    assert loaded is not None
    assert loaded.id == record.id
    assert loaded.workspace_id == ws
    assert loaded.name == "openai-prod"
    assert loaded.kind is APIKeyKind.INTEGRATION_TOKEN
    assert loaded.provider == "openai"
    assert loaded.key_prefix == "sk-o…"
    assert loaded.ciphertext == _CIPHERTEXT
    assert loaded.created_at == created
    assert loaded.last_used_at == used
    assert loaded.expires_at == expires
    assert loaded.key_version == 3
    assert loaded.rotated_at == rotated


def test_get_missing_returns_none(
    store: DbSecretStore, make_workspace: Callable[[], uuid.UUID]
) -> None:
    ws = make_workspace()
    assert store.get(ws, uuid.uuid4()) is None


def test_plaintext_never_persisted(
    store: DbSecretStore,
    factory: sessionmaker[Session],
    make_workspace: Callable[[], uuid.UUID],
) -> None:
    """Only ciphertext is stored: a real plaintext never appears in the row bytes."""
    ws = make_workspace()
    cipher = HmacAeadCipher(generate_key())
    plaintext = "sk-ant-PLAINTEXT-should-never-persist"
    record = _record(ws, ciphertext=cipher.encrypt(plaintext))
    store.add(record)

    with factory() as session:
        row = session.get(Secret, record.id)
        assert row is not None
        assert plaintext.encode() not in bytes(row.ciphertext)
    # And the vault decrypts it back through the DB store.
    vault = SecretVault(cipher=cipher, store=store)
    assert vault.get_secret(ws, record.id) == plaintext


# --------------------------------------------------------------------------- #
# Upsert (rotation) semantics                                                 #
# --------------------------------------------------------------------------- #


def test_add_upserts_on_repeated_id(
    store: DbSecretStore, make_workspace: Callable[[], uuid.UUID]
) -> None:
    ws = make_workspace()
    created = datetime(2026, 1, 1, tzinfo=UTC)
    record = _record(ws, created_at=created, updated_at=created, key_version=1)
    store.add(record)

    # Rotate in place: new ciphertext + bumped key_version + fresh updated_at,
    # created_at unchanged (mirrors SecretVault.rotate_secret / rewrap_all).
    rotated_at = datetime(2026, 2, 1, tzinfo=UTC)
    record.ciphertext = b"\x02rewrapped-blob"
    record.key_version = 2
    record.rotated_at = rotated_at
    record.updated_at = rotated_at
    store.add(record)

    loaded = store.get(ws, record.id)
    assert loaded is not None
    assert loaded.ciphertext == b"\x02rewrapped-blob"
    assert loaded.key_version == 2
    assert loaded.rotated_at == rotated_at
    assert loaded.created_at == created  # anchor preserved
    assert loaded.updated_at == rotated_at
    assert len(store.list(ws)) == 1  # upsert, not insert


def test_upsert_keeps_updated_at_when_rewrap_leaves_it(
    store: DbSecretStore, make_workspace: Callable[[], uuid.UUID]
) -> None:
    """rewrap_all mutates ciphertext but not updated_at; the column's onupdate
    must not silently bump it (parity with the in-place in-memory store)."""
    ws = make_workspace()
    stamp = datetime(2026, 3, 3, tzinfo=UTC)
    record = _record(ws, created_at=stamp, updated_at=stamp)
    store.add(record)

    record.ciphertext = b"\x02rewrapped-only"
    record.key_version = 7
    # updated_at deliberately left as the original stamp.
    store.add(record)

    loaded = store.get(ws, record.id)
    assert loaded is not None
    assert loaded.updated_at == stamp
    assert loaded.key_version == 7


# --------------------------------------------------------------------------- #
# Per-workspace isolation                                                     #
# --------------------------------------------------------------------------- #


def test_cross_tenant_get_returns_none(
    store: DbSecretStore, make_workspace: Callable[[], uuid.UUID]
) -> None:
    ws_a = make_workspace()
    ws_b = make_workspace()
    record = _record(ws_a)
    store.add(record)
    assert store.get(ws_a, record.id) is not None
    assert store.get(ws_b, record.id) is None  # same id, wrong tenant


def test_list_is_workspace_scoped_and_ordered(
    store: DbSecretStore, make_workspace: Callable[[], uuid.UUID]
) -> None:
    ws_a = make_workspace()
    ws_b = make_workspace()
    base = datetime(2026, 1, 1, tzinfo=UTC)
    first = _record(ws_a, name="first", created_at=base, updated_at=base)
    second = _record(
        ws_a,
        name="second",
        created_at=base + timedelta(minutes=5),
        updated_at=base + timedelta(minutes=5),
    )
    other = _record(ws_b, name="other")
    store.add(second)
    store.add(first)
    store.add(other)

    listed = store.list(ws_a)
    assert [r.name for r in listed] == ["first", "second"]  # oldest first
    assert {r.workspace_id for r in listed} == {ws_a}
    assert [r.name for r in store.list(ws_b)] == ["other"]


def test_remove_is_workspace_scoped(
    store: DbSecretStore, make_workspace: Callable[[], uuid.UUID]
) -> None:
    ws_a = make_workspace()
    ws_b = make_workspace()
    record = _record(ws_a)
    store.add(record)

    assert store.remove(ws_b, record.id) is False  # cross-tenant no-op
    assert store.get(ws_a, record.id) is not None
    assert store.remove(ws_a, record.id) is True
    assert store.get(ws_a, record.id) is None
    assert store.remove(ws_a, record.id) is False  # already gone


# --------------------------------------------------------------------------- #
# Constraints + durability                                                    #
# --------------------------------------------------------------------------- #


def test_workspace_fk_is_enforced(
    store: DbSecretStore, make_workspace: Callable[[], uuid.UUID]
) -> None:
    """A secret for a non-existent workspace is rejected (schema-enforced scope)."""
    ghost = uuid.uuid4()  # never inserted as a workspace
    with pytest.raises(IntegrityError):
        store.add(_record(ghost))


def test_all_records_spans_workspaces(
    store: DbSecretStore, make_workspace: Callable[[], uuid.UUID]
) -> None:
    ws_a = make_workspace()
    ws_b = make_workspace()
    store.add(_record(ws_a, name="a"))
    store.add(_record(ws_b, name="b"))
    names = {r.name for r in store.all_records()}
    assert names == {"a", "b"}


def test_persists_across_store_instances(
    factory: sessionmaker[Session], make_workspace: Callable[[], uuid.UUID]
) -> None:
    ws = make_workspace()
    record = _record(ws, name="durable")
    DbSecretStore(factory).add(record)

    # A brand-new instance sees the durable row.
    reloaded = DbSecretStore(factory).get(ws, record.id)
    assert reloaded is not None
    assert reloaded.name == "durable"


# --------------------------------------------------------------------------- #
# Vault semantics through the DB store (SecretNotFound / SecretExpired)        #
# --------------------------------------------------------------------------- #


def test_vault_raises_not_found_through_db_store(
    store: DbSecretStore, make_workspace: Callable[[], uuid.UUID]
) -> None:
    ws = make_workspace()
    vault = SecretVault(cipher=HmacAeadCipher(generate_key()), store=store)
    with pytest.raises(SecretNotFoundError):
        vault.get_secret(ws, uuid.uuid4())


def test_vault_read_time_expiry_through_db_store(
    store: DbSecretStore, make_workspace: Callable[[], uuid.UUID]
) -> None:
    ws = make_workspace()
    cipher = HmacAeadCipher(generate_key())
    vault = SecretVault(cipher=cipher, store=store)
    past = datetime.now(UTC) - timedelta(days=1)
    info = vault.put_secret(
        workspace_id=ws,
        name="expiring",
        secret="sk-EXPIRING-value-000",
        kind=APIKeyKind.MODEL_PROVIDER,
        expires_at=past,
    )
    with pytest.raises(SecretExpiredError):
        vault.get_secret(ws, info.id)
    # raw_record still yields the (encrypted) row for rotation.
    assert vault.raw_record(ws, info.id).ciphertext  # opaque bytes, non-empty


def test_vault_put_list_delete_round_trip_through_db_store(
    store: DbSecretStore, make_workspace: Callable[[], uuid.UUID]
) -> None:
    ws = make_workspace()
    vault = SecretVault(cipher=HmacAeadCipher(generate_key()), store=store)
    info = vault.put_secret(
        workspace_id=ws,
        name="listed",
        secret="sk-LISTED-value-000",
        kind=APIKeyKind.MCP_TOKEN,
        provider="notion",
    )
    listed = vault.list_secrets(ws)
    assert [i.name for i in listed] == ["listed"]
    assert listed[0].provider == "notion"
    vault.delete_secret(ws, info.id)
    assert vault.list_secrets(ws) == []
    with pytest.raises(SecretNotFoundError):
        vault.delete_secret(ws, info.id)


# --------------------------------------------------------------------------- #
# Parity with the in-memory store (same protocol, identical behaviour)        #
# --------------------------------------------------------------------------- #


def test_matches_in_memory_store_behaviour(
    store: DbSecretStore, make_workspace: Callable[[], uuid.UUID]
) -> None:
    from forge_api.auth.vault import InMemorySecretStore

    ws = make_workspace()
    mem = InMemorySecretStore()
    payloads = [_record(ws, name=f"k{i}") for i in range(3)]
    for payload in payloads:
        store.add(payload)
        mem.add(payload)

    assert {r.id for r in store.list(ws)} == {r.id for r in mem.list(ws)}
    target = payloads[1]
    assert (store.get(ws, target.id) is None) == (mem.get(ws, target.id) is None)
    assert store.remove(ws, target.id) == mem.remove(ws, target.id)
    assert {r.id for r in store.list(ws)} == {r.id for r in mem.list(ws)}
