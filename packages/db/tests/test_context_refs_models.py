"""Context refs + fact feedback models and migration 0044 (spec J §2.5)."""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Engine, StaticPool, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

import forge_db.models  # noqa: F401  (registers all models)
from forge_db.base import Base
from forge_db.models import AgentRun, Workspace, WorldEntity, WorldFact, WorldFactFeedback

DB_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    engine.dispose()


def _seed(factory: sessionmaker[Session]) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    workspace_id = uuid.uuid4()
    with factory() as session:
        session.add(Workspace(id=workspace_id, name="WS", slug=f"ws-{workspace_id.hex[:8]}"))
        entity = WorldEntity(workspace_id=workspace_id, label="Acme")
        session.add(entity)
        session.flush()
        fact = WorldFact(
            workspace_id=workspace_id,
            subject_id=entity.id,
            phrase="is based in",
            value="Berlin",
            layer="memory",
            source_kind="person",
            actor="u1",
            dedup_key="k" * 64,
        )
        run = AgentRun(workspace_id=workspace_id)
        session.add_all([fact, run])
        session.flush()
        ids = (workspace_id, fact.id, run.id)
        session.commit()
    return ids


def test_agent_run_context_refs_default_and_roundtrip(factory: sessionmaker[Session]) -> None:
    workspace_id, fact_id, run_id = _seed(factory)
    with factory() as session:
        run = session.get(AgentRun, run_id)
        assert run is not None
        assert run.context_refs == []
        run.context_refs = [{"kind": "memory", "id": str(fact_id)}, {"kind": "chunk", "id": "c1"}]
        session.commit()
    with factory() as session:
        loaded = session.get(AgentRun, run_id)
        assert loaded is not None
        assert loaded.context_refs == [
            {"kind": "memory", "id": str(fact_id)},
            {"kind": "chunk", "id": "c1"},
        ]
        assert loaded.workspace_id == workspace_id


def test_feedback_unique_per_run_and_fact(factory: sessionmaker[Session]) -> None:
    workspace_id, fact_id, run_id = _seed(factory)
    with factory() as session:
        session.add(
            WorldFactFeedback(
                workspace_id=workspace_id, fact_id=fact_id, run_id=run_id, outcome="reinforced"
            )
        )
        session.commit()
    with factory() as session:
        session.add(
            WorldFactFeedback(
                workspace_id=workspace_id, fact_id=fact_id, run_id=run_id, outcome="weakened"
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_feedback_outcome_check(factory: sessionmaker[Session]) -> None:
    workspace_id, fact_id, run_id = _seed(factory)
    with factory() as session:
        session.add(
            WorldFactFeedback(
                workspace_id=workspace_id, fact_id=fact_id, run_id=run_id, outcome="bogus"
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


# --- migration 0044 (SQLite via Alembic) ------------------------------------- #


@pytest.fixture
def alembic_config(tmp_path: Path) -> Config:
    cfg = Config(str(DB_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(DB_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{tmp_path / 'forge_test.db'}")
    return cfg


def _schema_state(url: str) -> tuple[bool, bool]:
    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        has_table = "world_fact_feedback" in tables
        has_column = "agent_run" in tables and "context_refs" in {
            column["name"] for column in inspector.get_columns("agent_run")
        }
        return has_table, has_column
    finally:
        engine.dispose()


def test_migration_0044_up_and_down(alembic_config: Config) -> None:
    url = alembic_config.get_main_option("sqlalchemy.url")
    assert url is not None
    # The chain (0044 included) upgrades cleanly to head...
    command.upgrade(alembic_config, "head")
    assert _schema_state(url) == (True, True)
    # ...and 0044's own downgrade removes exactly what it introduced. (The
    # metadata-driven baseline also creates later tables, so absence is
    # asserted after the module downgrade, not at the 0043 stamp — the
    # same pattern the world-model Postgres lane uses for 0041.)
    command.downgrade(alembic_config, "0043_agent_run_event")
    assert _schema_state(url) == (False, False)
    command.upgrade(alembic_config, "head")
    assert _schema_state(url) == (True, True)


def test_migration_0044_module_roundtrip_on_sqlite(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'direct.db'}")
    try:
        Base.metadata.create_all(engine)
        feedback_table = Base.metadata.tables["world_fact_feedback"]
        Base.metadata.drop_all(bind=engine, tables=[feedback_table])
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE agent_run DROP COLUMN context_refs"))
        assert _schema_state(str(engine.url)) == (False, False)
        module = _load_migration()
        with engine.begin() as connection:
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                module.upgrade()
        assert _schema_state(str(engine.url)) == (True, True)
        with engine.begin() as connection:
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                module.downgrade()
        assert _schema_state(str(engine.url)) == (False, False)
    finally:
        engine.dispose()


# --- migration 0044 on Postgres ---------------------------------------------- #


def _load_migration() -> ModuleType:
    path = DB_ROOT / "migrations" / "versions" / "0044_agent_run_context_refs.py"
    spec = importlib.util.spec_from_file_location("migration_0044", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _engine_state(engine: Engine) -> tuple[bool, bool]:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    has_table = "world_fact_feedback" in tables
    has_column = "agent_run" in tables and "context_refs" in {
        column["name"] for column in inspector.get_columns("agent_run")
    }
    return has_table, has_column


def test_migration_0044_on_postgres(pg_engine: Engine) -> None:
    module = _load_migration()
    assert module.revision == "0044_agent_run_context_refs"
    assert module.down_revision == "0043_agent_run_event"
    Base.metadata.create_all(pg_engine)
    try:
        feedback_table = Base.metadata.tables["world_fact_feedback"]
        Base.metadata.drop_all(bind=pg_engine, tables=[feedback_table])
        with pg_engine.begin() as connection:
            connection.execute(text("ALTER TABLE agent_run DROP COLUMN IF EXISTS context_refs"))
        assert _engine_state(pg_engine) == (False, False)
        with pg_engine.begin() as connection:
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                module.upgrade()
        assert _engine_state(pg_engine) == (True, True)
        inspector = inspect(pg_engine)
        uniques = {
            tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints("world_fact_feedback")
        }
        assert ("run_id", "fact_id") in uniques
        checks = {
            constraint["name"]
            for constraint in inspector.get_check_constraints("world_fact_feedback")
        }
        # The metadata naming convention prefixes check names on Postgres.
        assert any(
            name is not None and name.endswith("world_fact_feedback_outcome") for name in checks
        )
        foreign_keys = {
            (tuple(fk["constrained_columns"]), fk["referred_table"])
            for fk in inspector.get_foreign_keys("world_fact_feedback")
        }
        assert (("fact_id",), "world_fact") in foreign_keys
        assert (("run_id",), "agent_run") in foreign_keys

        with pg_engine.begin() as connection:
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                module.downgrade()
        assert _engine_state(pg_engine) == (False, False)
    finally:
        with pg_engine.begin() as connection:
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                module.upgrade()  # leave the schema whole
