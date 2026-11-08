"""Postgres lane (spec §8): tombstone trigger, live-dedup index, migration.

These request the root ``pg_engine`` fixture and skip with its PARKED reason
when no Postgres is available — the SQLite suite cannot prove trigger or
concurrent-index behaviour (criteria 1 and 8 live here).
"""

from __future__ import annotations

import importlib.util
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from conftest import T0
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

import forge_db.models  # noqa: F401
from forge_db.base import Base
from forge_db.models import Workspace
from forge_world import (
    InvalidatedReason,
    Layer,
    SourceKind,
    Statement,
    WorldRepository,
)

WORLD_TABLES = [
    "world_class",
    "world_relation",
    "world_entity",
    "world_fact",
    "world_evidence",
    "world_extraction_drop",
    "world_proposal",
    "world_entity_merge",
    "world_derivation",
    "world_decision",
]


@pytest.fixture
def pg_factory(pg_engine: Engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)


def _workspace(factory: sessionmaker[Session]) -> uuid.UUID:
    workspace_id = uuid.uuid4()
    with factory() as session:
        session.add(Workspace(id=workspace_id, name="PG WS", slug=f"pg-{workspace_id.hex[:8]}"))
        session.commit()
    return workspace_id


def _insert_fact(
    repo: WorldRepository, workspace_id: uuid.UUID, *, now: datetime = T0
) -> tuple[uuid.UUID, bool]:
    session = repo.session
    entity = repo.insert_entity(workspace_id, label="Acme", now=now)
    statement = Statement(
        subject_id=entity.id,
        phrase="based in",
        predicate_key=None,
        object_id=None,
        value="Berlin",
        layer=Layer.OPEN,
    )
    fact, created = repo.insert_fact(
        workspace_id, statement, source_kind=SourceKind.PERSON, actor="u1", now=now
    )
    session.commit()
    return fact.id, created


def test_tombstone_trigger_refuses_other_updates_and_deletes(
    pg_factory: sessionmaker[Session],
) -> None:
    workspace_id = _workspace(pg_factory)
    session = pg_factory()
    repo = WorldRepository(session)
    fact_id, _ = _insert_fact(repo, workspace_id)

    with pytest.raises(DBAPIError):
        session.execute(
            text("UPDATE world_fact SET phrase = 'elsewhere' WHERE id = :id"),
            {"id": fact_id},
        )
        session.commit()
    session.rollback()

    with pytest.raises(DBAPIError):
        session.execute(
            text("UPDATE world_fact SET value = 'Munich' WHERE id = :id"), {"id": fact_id}
        )
        session.commit()
    session.rollback()

    with pytest.raises(DBAPIError):
        session.execute(text("DELETE FROM world_fact WHERE id = :id"), {"id": fact_id})
        session.commit()
    session.rollback()

    # A tombstone stamp smuggling another column change: only the
    # to_jsonb diff clause can refuse this one (minor 18).
    with pytest.raises(DBAPIError):
        session.execute(
            text(
                "UPDATE world_fact SET invalidated_at = now(), "
                "invalidated_reason = 'retracted', value = 'Munich' WHERE id = :id"
            ),
            {"id": fact_id},
        )
        session.commit()
    session.rollback()

    # The one permitted update: the tombstone stamp.
    repo.invalidate_fact(workspace_id, fact_id, reason=InvalidatedReason.RETRACTED, now=T0)
    session.commit()

    # A second stamp is refused by the trigger as well.
    with pytest.raises(DBAPIError):
        session.execute(
            text(
                "UPDATE world_fact SET invalidated_at = now(), "
                "invalidated_reason = 'retracted' WHERE id = :id"
            ),
            {"id": fact_id},
        )
        session.commit()
    session.rollback()
    session.close()


def test_append_only_evidence_trigger(pg_factory: sessionmaker[Session]) -> None:
    workspace_id = _workspace(pg_factory)
    session = pg_factory()
    repo = WorldRepository(session)
    fact_id, _ = _insert_fact(repo, workspace_id)
    evidence = repo.insert_evidence(
        workspace_id,
        fact_id=fact_id,
        source_ref="doc.md",
        chunk_index=0,
        quote="Acme is in Berlin",
        start_offset=0,
        end_offset=17,
        doc_time=None,
        now=T0,
    )
    session.commit()

    with pytest.raises(DBAPIError):
        session.execute(
            text("UPDATE world_evidence SET quote = 'x' WHERE id = :id"),
            {"id": evidence.id},
        )
        session.commit()
    session.rollback()
    with pytest.raises(DBAPIError):
        session.execute(text("DELETE FROM world_evidence WHERE id = :id"), {"id": evidence.id})
        session.commit()
    session.rollback()
    session.close()


def test_partial_unique_index_under_concurrent_sessions(
    pg_factory: sessionmaker[Session],
) -> None:
    workspace_id = _workspace(pg_factory)
    with pg_factory() as session:
        entity = WorldRepository(session).insert_entity(workspace_id, label="Acme", now=T0)
        entity_id = entity.id
        session.commit()

    statement = Statement(
        subject_id=entity_id,
        phrase="based in",
        predicate_key=None,
        object_id=None,
        value="Berlin",
        layer=Layer.OPEN,
    )
    results: dict[str, tuple[uuid.UUID, bool]] = {}
    errors: dict[str, Exception] = {}

    def worker(name: str) -> None:
        try:
            with pg_factory() as session:
                fact, created = WorldRepository(session).insert_fact(
                    workspace_id,
                    statement,
                    source_kind=SourceKind.PERSON,
                    actor="u1",
                    now=datetime.now(UTC),
                )
                session.commit()
                results[name] = (fact.id, created)
        except Exception as exc:  # surfaced by the assertions below
            errors[name] = exc

    threads = [threading.Thread(target=worker, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == {}
    created_flags = sorted(created for _id, created in results.values())
    assert created_flags == [False, True]  # exactly one insert won
    assert len({fact_id for fact_id, _created in results.values()}) == 1

    with pg_factory() as session:
        count = session.execute(
            text(
                "SELECT count(*) FROM world_fact "
                "WHERE workspace_id = :ws AND invalidated_at IS NULL"
            ),
            {"ws": workspace_id},
        ).scalar_one()
    assert count == 1


def _load_migration() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[2]
        / "db"
        / "migrations"
        / "versions"
        / "0041_world_model.py"
    )
    spec = importlib.util.spec_from_file_location("world_migration_0041", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_upgrades_and_downgrades_cleanly(pg_engine: Engine) -> None:
    Base.metadata.create_all(pg_engine)
    # 0044's feedback table FKs world_fact, so it must go before 0041's
    # tables can drop; it is not one of 0041's own tables.
    world_tables = [Base.metadata.tables[name] for name in [*WORLD_TABLES, "world_fact_feedback"]]
    Base.metadata.drop_all(bind=pg_engine, tables=world_tables)
    assert not (set(WORLD_TABLES) & set(inspect(pg_engine).get_table_names()))

    module = _load_migration()
    assert module.down_revision == "0040_self_eval_baseline"
    try:
        with pg_engine.begin() as connection:
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                module.upgrade()
        assert set(WORLD_TABLES) <= set(inspect(pg_engine).get_table_names())

        with pg_engine.begin() as connection:
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                module.downgrade()
        assert not (set(WORLD_TABLES) & set(inspect(pg_engine).get_table_names()))
    finally:
        Base.metadata.create_all(pg_engine)  # leave the schema whole
