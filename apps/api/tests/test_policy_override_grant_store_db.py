"""Postgres integration tests for :class:`DbGrantStore` (F36 J5).

Exercises the DB-backed policy-override grant store against a real pgvector
Postgres via the shared ``pg_engine`` fixture (root ``conftest.py``): the
``mint`` / async ``consume`` / ``all`` grant-store seam end-to-end — a full
``PolicyOverrideGrant`` round-trip, the single-active invariant (idempotent
mint + the ``uq_active_override`` partial-unique storage boundary), atomic
single-use ``consume``, TTL expiry (expired denies + does not block a re-mint),
fingerprint/agent-run mismatch denials, workspace derivation from the bound
``agent_run``, durability across store instances, and structural conformance to
the same seam the in-memory store implements.

Skips cleanly (parked) when no Postgres is reachable; runs under
``FORGE_TEST_DATABASE_URL`` (pgvector :5433) in the gate. Each behaviour mirrors
the in-memory contract, so both backends are proven to satisfy the same seam
identically.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from forge_api.services.policy_override_grant_store_db import (
    DbGrantStore,
    UnknownAgentRunError,
)
from forge_approval.models import PolicyOverrideGrant
from forge_approval.providers.policy_override import (
    GrantStore,
    InMemoryGrantStore,
    PolicyOverrideGate,
    action_fingerprint,
)
from forge_db.base import Base
from forge_db.models import AgentRun, ApprovalRequest, User, Workspace
from forge_db.models.enums import ApprovalGate

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("pg_engine")]

FINGERPRINT = action_fingerprint(
    {"tool": "shell", "action": "run", "arguments": {"cmd": "rm -rf /tmp/x"}}
)
OTHER_FINGERPRINT = action_fingerprint(
    {"tool": "shell", "action": "run", "arguments": {"cmd": "ls"}}
)


@pytest.fixture
def factory(pg_engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    try:
        yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(pg_engine)


@pytest.fixture
def seed(factory: sessionmaker[Session]) -> dict[str, uuid.UUID]:
    """A workspace + admin user + agent_run + approval_request (the grant FKs)."""
    ws = uuid.uuid4()
    admin = uuid.uuid4()
    run = uuid.uuid4()
    request = uuid.uuid4()
    with factory() as session:
        session.add(Workspace(id=ws, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}"))
        session.flush()
        session.add(
            User(id=admin, workspace_id=ws, email=f"a-{admin.hex[:6]}@acme.dev", name="Admin")
        )
        session.add(AgentRun(id=run, workspace_id=ws))
        session.add(
            ApprovalRequest(id=request, workspace_id=ws, gate=ApprovalGate.POLICY_OVERRIDE)
        )
        session.commit()
    return {"ws": ws, "admin": admin, "run": run, "request": request}


@pytest.fixture
def store(factory: sessionmaker[Session]) -> DbGrantStore:
    return DbGrantStore(factory)


def _grant(
    seed: dict[str, uuid.UUID],
    *,
    fingerprint: str = FINGERPRINT,
    expires_in: timedelta = timedelta(minutes=15),
    grant_id: uuid.UUID | None = None,
) -> PolicyOverrideGrant:
    return PolicyOverrideGrant(
        id=grant_id or uuid.uuid4(),
        approval_request_id=seed["request"],
        agent_run_id=seed["run"],
        action_fingerprint=fingerprint,
        granted_by=seed["admin"],
        expires_at=datetime.now(UTC) + expires_in,
    )


# --------------------------------------------------------------------------- #
# Protocol conformance                                                        #
# --------------------------------------------------------------------------- #


def test_store_satisfies_grant_store_and_gate_protocols(store: DbGrantStore) -> None:
    assert isinstance(store, GrantStore)
    assert isinstance(store, PolicyOverrideGate)  # the consume-only resume contract


# --------------------------------------------------------------------------- #
# mint + round-trip                                                           #
# --------------------------------------------------------------------------- #


def test_mint_round_trips_every_field(
    store: DbGrantStore, seed: dict[str, uuid.UUID]
) -> None:
    grant = _grant(seed)
    returned = store.mint(grant)
    assert returned.id == grant.id
    assert returned.created_at is not None  # server-default timestamp populated

    stored = store.all()
    assert len(stored) == 1
    row = stored[0]
    assert row.id == grant.id
    assert row.approval_request_id == seed["request"]
    assert row.agent_run_id == seed["run"]
    assert row.action_fingerprint == FINGERPRINT
    assert row.granted_by == seed["admin"]
    assert row.consumed is False
    assert row.expires_at is not None and row.expires_at.tzinfo is not None
    assert row.expires_at == grant.expires_at


def test_mint_derives_workspace_from_agent_run(
    store: DbGrantStore, seed: dict[str, uuid.UUID], factory: sessionmaker[Session]
) -> None:
    from sqlalchemy import select

    from forge_db.models import PolicyOverrideGrant as Row

    store.mint(_grant(seed))
    with factory() as session:
        row = session.scalars(select(Row)).one()
    assert row.workspace_id == seed["ws"]


def test_mint_unknown_agent_run_raises(
    store: DbGrantStore, seed: dict[str, uuid.UUID]
) -> None:
    ghost = seed | {"run": uuid.uuid4()}
    with pytest.raises(UnknownAgentRunError):
        store.mint(_grant(ghost))


# --------------------------------------------------------------------------- #
# single-active invariant                                                     #
# --------------------------------------------------------------------------- #


def test_mint_is_idempotent_while_active(
    store: DbGrantStore, seed: dict[str, uuid.UUID]
) -> None:
    first = store.mint(_grant(seed))
    second = store.mint(_grant(seed))  # a *different* grant object, same (run, fp)
    assert second.id == first.id  # existing active grant returned, not duplicated
    assert len(store.all()) == 1


# --------------------------------------------------------------------------- #
# consume: single-use + atomic                                                #
# --------------------------------------------------------------------------- #


async def test_consume_is_single_use(
    store: DbGrantStore, seed: dict[str, uuid.UUID]
) -> None:
    store.mint(_grant(seed))
    assert await store.consume(agent_run_id=seed["run"], action_fingerprint=FINGERPRINT)
    # ... and never again for the same grant.
    assert not await store.consume(
        agent_run_id=seed["run"], action_fingerprint=FINGERPRINT
    )


async def test_consume_without_grant_denies(
    store: DbGrantStore, seed: dict[str, uuid.UUID]
) -> None:
    assert not await store.consume(
        agent_run_id=seed["run"], action_fingerprint=FINGERPRINT
    )


async def test_consume_fingerprint_mismatch_denies(
    store: DbGrantStore, seed: dict[str, uuid.UUID]
) -> None:
    store.mint(_grant(seed))
    assert not await store.consume(
        agent_run_id=seed["run"], action_fingerprint=OTHER_FINGERPRINT
    )
    # The original grant is untouched by the mismatch and still consumable once.
    assert await store.consume(agent_run_id=seed["run"], action_fingerprint=FINGERPRINT)


async def test_consume_agent_run_mismatch_denies(
    store: DbGrantStore, seed: dict[str, uuid.UUID]
) -> None:
    store.mint(_grant(seed))
    assert not await store.consume(
        agent_run_id=uuid.uuid4(), action_fingerprint=FINGERPRINT
    )


# --------------------------------------------------------------------------- #
# TTL expiry                                                                   #
# --------------------------------------------------------------------------- #


async def test_expired_grant_denies_consume(
    store: DbGrantStore, seed: dict[str, uuid.UUID]
) -> None:
    store.mint(_grant(seed, expires_in=timedelta(minutes=-1)))
    assert not await store.consume(
        agent_run_id=seed["run"], action_fingerprint=FINGERPRINT
    )


async def test_remint_after_expiry_yields_a_fresh_usable_grant(
    store: DbGrantStore, seed: dict[str, uuid.UUID]
) -> None:
    """An expired-but-unconsumed grant must not block a re-mint (index reap)."""
    expired = store.mint(_grant(seed, expires_in=timedelta(minutes=-1)))
    fresh = store.mint(_grant(seed, expires_in=timedelta(minutes=15)))
    assert fresh.id != expired.id  # a genuinely new active grant
    # The fresh grant is consumable; the stale one was reaped out of the way.
    assert await store.consume(agent_run_id=seed["run"], action_fingerprint=FINGERPRINT)


# --------------------------------------------------------------------------- #
# single-active across consumption + storage boundary                         #
# --------------------------------------------------------------------------- #


async def test_remint_after_consume_creates_new_active_grant(
    store: DbGrantStore, seed: dict[str, uuid.UUID]
) -> None:
    first = store.mint(_grant(seed))
    assert await store.consume(agent_run_id=seed["run"], action_fingerprint=FINGERPRINT)
    second = store.mint(_grant(seed))  # once consumed, a fresh active grant may mint
    assert second.id != first.id
    assert len(store.all()) == 2  # both rows persist (one consumed, one active)
    assert await store.consume(agent_run_id=seed["run"], action_fingerprint=FINGERPRINT)


def test_all_orders_by_insertion(
    store: DbGrantStore, seed: dict[str, uuid.UUID]
) -> None:
    # Two distinct fingerprints so both stay active (single-active is per-fp).
    g1 = store.mint(_grant(seed, fingerprint=FINGERPRINT))
    g2 = store.mint(_grant(seed, fingerprint=OTHER_FINGERPRINT))
    assert [g.id for g in store.all()] == [g1.id, g2.id]


# --------------------------------------------------------------------------- #
# durability                                                                   #
# --------------------------------------------------------------------------- #


async def test_persists_across_store_instances(
    factory: sessionmaker[Session], seed: dict[str, uuid.UUID]
) -> None:
    first = DbGrantStore(factory)
    first.mint(_grant(seed))

    second = DbGrantStore(factory)
    assert len(second.all()) == 1
    # A grant minted through one instance is consumable through another.
    assert await second.consume(
        agent_run_id=seed["run"], action_fingerprint=FINGERPRINT
    )


# --------------------------------------------------------------------------- #
# Parity with the in-memory store (same seam, identical behaviour)            #
# --------------------------------------------------------------------------- #


async def test_matches_in_memory_store_behaviour(
    store: DbGrantStore, seed: dict[str, uuid.UUID]
) -> None:
    mem = InMemoryGrantStore()

    db_first = store.mint(_grant(seed, grant_id=uuid.uuid4()))
    mem_first = mem.mint(_grant(seed, grant_id=db_first.id))

    # Idempotent-while-active: both return the existing grant, not a duplicate.
    assert store.mint(_grant(seed)).id == db_first.id
    assert mem.mint(_grant(seed, grant_id=mem_first.id)).id == mem_first.id

    # Single-use: True exactly once on both backends, then False.
    assert (
        await store.consume(agent_run_id=seed["run"], action_fingerprint=FINGERPRINT)
    ) == (await mem.consume(agent_run_id=seed["run"], action_fingerprint=FINGERPRINT))
    assert (
        await store.consume(agent_run_id=seed["run"], action_fingerprint=FINGERPRINT)
    ) == (await mem.consume(agent_run_id=seed["run"], action_fingerprint=FINGERPRINT))
