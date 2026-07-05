"""Postgres integration tests for :class:`DbLinkRepository` (F18).

Exercises the DB-backed PM-sync link repository against a real pgvector Postgres
via the shared ``pg_engine`` fixture (root ``conftest.py``): the
``LinkRepository`` protocol end-to-end — a full :class:`LinkRecord` round-trip
(every field, including the ``conflict_detail`` JSONB, the sync watermarks/hashes
and the timezone-aware ``last_synced_at`` / ``external_updated_at_at_sync``),
``upsert`` insert-vs-update keyed by id, connection-scoped
``get_by_forge_task`` / ``get_by_external``, ``list_by_state`` filtering +
connection scoping, ``delete`` (idempotent), the ``uq_pm_task_link_conn_task`` /
``uq_pm_task_link_conn_extid`` storage-boundary constraints, returned-copy
isolation, durability across repository instances, and structural conformance to
the same protocol the in-memory store implements. Skips cleanly (parked) when no
Postgres is reachable; runs under ``FORGE_TEST_DATABASE_URL`` (pgvector :5433) in
the gate.

Each behaviour mirrors the in-memory contract, so both backends are proven to
satisfy the same protocol identically.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from forge_api.services.pm_link_repository_db import DbLinkRepository
from forge_contracts.pm import PMProvider as ContractsProvider
from forge_contracts.pm import PMSyncState as ContractsSyncState
from forge_db.base import Base
from forge_db.models import PMConnection, Project, Task, Workspace
from forge_db.models.enums import PMProvider as DbProvider
from forge_integrations.pm.sync_engine import InMemoryLinkRepository, LinkRecord

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("pg_engine")]

_PROTOCOL_METHODS = (
    "get",
    "get_by_forge_task",
    "get_by_external",
    "upsert",
    "delete",
    "list_by_state",
)


@pytest.fixture
def factory(pg_engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    try:
        yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(pg_engine)


@pytest.fixture
def seed(factory: sessionmaker[Session]) -> dict[str, object]:
    """A workspace + project + two PM connections + a pool of tasks (plus an
    isolated second workspace), so link rows have real FK parents."""
    ws = uuid4()
    other_ws = uuid4()
    project_id = uuid4()
    conn = uuid4()
    conn2 = uuid4()
    tasks = [uuid4() for _ in range(8)]
    with factory() as session:
        session.add(Workspace(id=ws, name="Acme", slug=f"acme-{uuid4().hex[:8]}"))
        session.add(Workspace(id=other_ws, name="Other", slug=f"other-{uuid4().hex[:8]}"))
        session.flush()
        session.add(
            Project(id=project_id, workspace_id=ws, name="API", key=f"API{uuid4().hex[:4]}")
        )
        session.flush()
        session.add(
            PMConnection(
                id=conn,
                workspace_id=ws,
                provider=DbProvider.JIRA,
                name="jira",
                project_id=project_id,
                external_project_key="ABC",
                external_project_id="10000",
            )
        )
        session.add(
            PMConnection(
                id=conn2,
                workspace_id=ws,
                provider=DbProvider.LINEAR,
                name="linear",
                project_id=project_id,
                external_project_key="LIN",
                external_project_id="lin-1",
            )
        )
        session.flush()
        for i, tid in enumerate(tasks):
            session.add(
                Task(
                    id=tid,
                    workspace_id=ws,
                    project_id=project_id,
                    key=f"API-{i}",
                    title=f"Task {i}",
                )
            )
        session.commit()
    return {"ws": ws, "other_ws": other_ws, "conn": conn, "conn2": conn2, "tasks": tasks}


@pytest.fixture
def repo(factory: sessionmaker[Session]) -> DbLinkRepository:
    return DbLinkRepository(factory)


def _link(
    seed: dict[str, object],
    *,
    task_index: int = 0,
    connection: UUID | None = None,
    external_id: str | None = None,
    provider: ContractsProvider = ContractsProvider.jira,
    **kw: object,
) -> LinkRecord:
    tasks: list[UUID] = seed["tasks"]  # type: ignore[assignment]
    return LinkRecord(
        connection_id=connection or seed["conn"],  # type: ignore[arg-type]
        workspace_id=seed["ws"],  # type: ignore[arg-type]
        forge_task_id=tasks[task_index],
        provider=provider,
        external_id=external_id or f"ext-{uuid4().hex[:8]}",
        **kw,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# Protocol conformance                                                        #
# --------------------------------------------------------------------------- #


def test_repo_conforms_to_link_repository_protocol(repo: DbLinkRepository) -> None:
    mem = InMemoryLinkRepository()
    for name in _PROTOCOL_METHODS:
        assert callable(getattr(repo, name))
        assert callable(getattr(mem, name))


# --------------------------------------------------------------------------- #
# upsert + get round-trip                                                     #
# --------------------------------------------------------------------------- #


def test_upsert_then_get_round_trips_every_field(
    repo: DbLinkRepository, seed: dict[str, object]
) -> None:
    last_synced = datetime(2026, 7, 5, 12, 0, 0, 123456, tzinfo=UTC)
    ext_updated = datetime(2026, 7, 5, 11, 0, 0, 654321, tzinfo=UTC)
    link = _link(
        seed,
        task_index=0,
        external_id="EXT-1",
        provider=ContractsProvider.jira,
        external_key="ABC-1",
        external_url="https://jira.example/ABC-1",
        last_synced_at=last_synced,
        forge_version_at_sync=3,
        external_updated_at_at_sync=ext_updated,
        last_outbound_hash="out-hash",
        last_inbound_hash="in-hash",
        sync_state=ContractsSyncState.conflict,
        conflict_detail={"forge": {"v": 1}, "external": {"k": [1, 2, 3]}},
        last_error="boom",
    )
    returned = repo.upsert(link)
    # ``upsert`` returns the stored record verbatim (parity with the in-memory store).
    assert returned.id == link.id
    assert returned.external_id == "EXT-1"

    loaded = repo.get(link.id)
    assert loaded is not None
    assert loaded.id == link.id
    assert loaded.connection_id == seed["conn"]
    assert loaded.workspace_id == seed["ws"]
    assert loaded.forge_task_id == seed["tasks"][0]  # type: ignore[index]
    assert loaded.provider is ContractsProvider.jira
    assert loaded.external_id == "EXT-1"
    assert loaded.external_key == "ABC-1"
    assert loaded.external_url == "https://jira.example/ABC-1"
    assert loaded.last_synced_at == last_synced
    assert loaded.forge_version_at_sync == 3
    assert loaded.external_updated_at_at_sync == ext_updated
    assert loaded.last_outbound_hash == "out-hash"
    assert loaded.last_inbound_hash == "in-hash"
    assert loaded.sync_state is ContractsSyncState.conflict
    assert loaded.conflict_detail == {"forge": {"v": 1}, "external": {"k": [1, 2, 3]}}
    assert loaded.last_error == "boom"


def test_upsert_defaults_round_trip(repo: DbLinkRepository, seed: dict[str, object]) -> None:
    link = _link(seed, task_index=0, external_id="EXT-D")
    repo.upsert(link)
    loaded = repo.get(link.id)
    assert loaded is not None
    # ``LinkRecord`` string defaults are "" (NOT NULL columns), the rest None.
    assert loaded.external_key == ""
    assert loaded.external_url == ""
    assert loaded.last_synced_at is None
    assert loaded.forge_version_at_sync is None
    assert loaded.external_updated_at_at_sync is None
    assert loaded.last_outbound_hash is None
    assert loaded.last_inbound_hash is None
    assert loaded.conflict_detail is None
    assert loaded.last_error is None
    assert loaded.sync_state is ContractsSyncState.synced  # LinkRecord default


def test_upsert_updates_existing_by_id(repo: DbLinkRepository, seed: dict[str, object]) -> None:
    link = _link(seed, task_index=0, external_id="EXT-1", sync_state=ContractsSyncState.pending_out)
    repo.upsert(link)

    link.sync_state = ContractsSyncState.synced
    link.last_outbound_hash = "new-hash"
    link.external_key = "ABC-9"
    repo.upsert(link)

    loaded = repo.get(link.id)
    assert loaded is not None
    assert loaded.sync_state is ContractsSyncState.synced
    assert loaded.last_outbound_hash == "new-hash"
    assert loaded.external_key == "ABC-9"
    # An update keyed by id never inserts a second row.
    assert repo.list_by_state(seed["conn"], ContractsSyncState.pending_out) == []  # type: ignore[arg-type]
    assert len(repo.list_by_state(seed["conn"], ContractsSyncState.synced)) == 1  # type: ignore[arg-type]


def test_get_unknown_is_none(repo: DbLinkRepository) -> None:
    assert repo.get(uuid4()) is None


def test_returned_records_are_isolated_copies(
    repo: DbLinkRepository, seed: dict[str, object]
) -> None:
    link = _link(seed, task_index=0, external_id="iso", conflict_detail={"k": 1})
    repo.upsert(link)

    loaded = repo.get(link.id)
    assert loaded is not None
    loaded.external_id = "MUTATED"
    assert loaded.conflict_detail is not None
    loaded.conflict_detail["k"] = 999

    again = repo.get(link.id)
    assert again is not None
    assert again.external_id == "iso"
    assert again.conflict_detail == {"k": 1}


# --------------------------------------------------------------------------- #
# get_by_forge_task / get_by_external: connection-scoped                       #
# --------------------------------------------------------------------------- #


def test_get_by_forge_task_scoped_by_connection(
    repo: DbLinkRepository, seed: dict[str, object]
) -> None:
    tasks: list[UUID] = seed["tasks"]  # type: ignore[assignment]
    link = _link(seed, task_index=0, connection=seed["conn"], external_id="E1")  # type: ignore[arg-type]
    repo.upsert(link)

    found = repo.get_by_forge_task(seed["conn"], tasks[0])  # type: ignore[arg-type]
    assert found is not None and found.id == link.id
    # Foreign connection / different task -> absent.
    assert repo.get_by_forge_task(seed["conn2"], tasks[0]) is None  # type: ignore[arg-type]
    assert repo.get_by_forge_task(seed["conn"], tasks[1]) is None  # type: ignore[arg-type]


def test_get_by_external_scoped_by_connection(
    repo: DbLinkRepository, seed: dict[str, object]
) -> None:
    link = _link(seed, task_index=0, connection=seed["conn"], external_id="E-777")  # type: ignore[arg-type]
    repo.upsert(link)

    found = repo.get_by_external(seed["conn"], "E-777")  # type: ignore[arg-type]
    assert found is not None and found.id == link.id
    # Foreign connection / unknown external id -> absent.
    assert repo.get_by_external(seed["conn2"], "E-777") is None  # type: ignore[arg-type]
    assert repo.get_by_external(seed["conn"], "missing") is None  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# list_by_state: filtering + connection scope                                 #
# --------------------------------------------------------------------------- #


def test_list_by_state_filters_and_scopes_by_connection(
    repo: DbLinkRepository, seed: dict[str, object]
) -> None:
    conn: UUID = seed["conn"]  # type: ignore[assignment]
    conn2: UUID = seed["conn2"]  # type: ignore[assignment]
    synced = ContractsSyncState.synced
    l0 = _link(seed, task_index=0, connection=conn, external_id="A", sync_state=synced)
    l1 = _link(seed, task_index=1, connection=conn, external_id="B", sync_state=synced)
    l2 = _link(seed, task_index=2, connection=conn, external_id="C", sync_state=synced)
    lc = _link(
        seed,
        task_index=3,
        connection=conn,
        external_id="D",
        sync_state=ContractsSyncState.conflict,
    )
    # A synced link on a *different* connection must never appear under ``conn``.
    other = _link(seed, task_index=4, connection=conn2, external_id="E", sync_state=synced)
    for link in (l0, l1, l2, lc, other):
        repo.upsert(link)

    assert {r.id for r in repo.list_by_state(conn, ContractsSyncState.synced)} == {
        l0.id,
        l1.id,
        l2.id,
    }
    assert [r.id for r in repo.list_by_state(conn, ContractsSyncState.conflict)] == [lc.id]
    assert repo.list_by_state(conn, ContractsSyncState.error) == []
    assert [r.id for r in repo.list_by_state(conn2, ContractsSyncState.synced)] == [other.id]


# --------------------------------------------------------------------------- #
# delete                                                                      #
# --------------------------------------------------------------------------- #


def test_delete_removes_link_and_is_idempotent(
    repo: DbLinkRepository, seed: dict[str, object]
) -> None:
    link = _link(seed, task_index=0, external_id="E-del")
    repo.upsert(link)
    assert repo.get(link.id) is not None

    repo.delete(link.id)
    assert repo.get(link.id) is None
    # Idempotent: deleting an already-removed / unknown id is a no-op.
    repo.delete(link.id)
    repo.delete(uuid4())


# --------------------------------------------------------------------------- #
# Storage-boundary constraints + durability                                   #
# --------------------------------------------------------------------------- #


def test_duplicate_forge_task_link_raises(repo: DbLinkRepository, seed: dict[str, object]) -> None:
    """``uq_pm_task_link_conn_task`` blocks a second link for the same task."""
    repo.upsert(_link(seed, task_index=0, connection=seed["conn"], external_id="X1"))  # type: ignore[arg-type]
    with pytest.raises(IntegrityError):
        repo.upsert(_link(seed, task_index=0, connection=seed["conn"], external_id="X2"))  # type: ignore[arg-type]


def test_duplicate_external_id_link_raises(repo: DbLinkRepository, seed: dict[str, object]) -> None:
    """``uq_pm_task_link_conn_extid`` blocks two tasks mapping to one external id."""
    repo.upsert(_link(seed, task_index=0, connection=seed["conn"], external_id="SAME"))  # type: ignore[arg-type]
    with pytest.raises(IntegrityError):
        repo.upsert(_link(seed, task_index=1, connection=seed["conn"], external_id="SAME"))  # type: ignore[arg-type]


def test_persists_across_repository_instances(
    factory: sessionmaker[Session], seed: dict[str, object]
) -> None:
    first = DbLinkRepository(factory)
    link = _link(seed, task_index=0, external_id="dur")
    first.upsert(link)

    second = DbLinkRepository(factory)
    loaded = second.get(link.id)
    assert loaded is not None and loaded.external_id == "dur"


# --------------------------------------------------------------------------- #
# Parity with the in-memory store (same protocol, identical behaviour)        #
# --------------------------------------------------------------------------- #


def test_matches_in_memory_store_behaviour(repo: DbLinkRepository, seed: dict[str, object]) -> None:
    conn: UUID = seed["conn"]  # type: ignore[assignment]
    tasks: list[UUID] = seed["tasks"]  # type: ignore[assignment]
    mem = InMemoryLinkRepository()

    synced = ContractsSyncState.synced
    links = [
        _link(seed, task_index=0, connection=conn, external_id="p0", sync_state=synced),
        _link(
            seed,
            task_index=1,
            connection=conn,
            external_id="p1",
            sync_state=ContractsSyncState.conflict,
        ),
        _link(seed, task_index=2, connection=conn, external_id="p2", sync_state=synced),
    ]
    for link in links:
        repo.upsert(link.model_copy(deep=True))
        mem.upsert(link.model_copy(deep=True))

    # get agrees on both backends.
    for link in links:
        db_rec = repo.get(link.id)
        mem_rec = mem.get(link.id)
        assert db_rec is not None and mem_rec is not None
        assert db_rec.external_id == mem_rec.external_id
        assert db_rec.sync_state == mem_rec.sync_state

    # list_by_state agrees (order-independent; the protocol carries no ordering).
    assert {r.id for r in repo.list_by_state(conn, ContractsSyncState.synced)} == {
        r.id for r in mem.list_by_state(conn, ContractsSyncState.synced)
    }

    # get_by_external + get_by_forge_task agree.
    db_ext = repo.get_by_external(conn, "p1")
    mem_ext = mem.get_by_external(conn, "p1")
    assert db_ext is not None and mem_ext is not None and db_ext.id == mem_ext.id

    db_task = repo.get_by_forge_task(conn, tasks[0])
    mem_task = mem.get_by_forge_task(conn, tasks[0])
    assert db_task is not None and mem_task is not None
    assert db_task.id == mem_task.id == links[0].id
