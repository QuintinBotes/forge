"""Migration 0045 workflow lines and the station-visit model (spec K §4.3)."""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

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
from forge_db.models import (
    MarketplaceInstallation,
    Project,
    Task,
    WorkflowDefinition,
    WorkflowDefinitionSource,
    WorkflowRun,
    WorkflowStationVisit,
    Workspace,
)

DB_ROOT = Path(__file__).resolve().parent.parent
NEW_KEY = ["workspace_id", "registry_slug", "kind", "listing_slug"]
OLD_KEY = ["workspace_id", "registry_slug", "listing_slug"]


def _load_migration() -> ModuleType:
    path = DB_ROOT / "migrations" / "versions" / "0045_workflow_lines.py"
    spec = importlib.util.spec_from_file_location("migration_0045", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine: Engine, step: str) -> None:
    module = _load_migration()
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        getattr(module, step)()


def _seed_run(session: Session, state: str) -> tuple[uuid.UUID, uuid.UUID]:
    ws = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    session.add(ws)
    session.flush()
    project = Project(workspace_id=ws.id, name="Core", key=f"C{uuid.uuid4().hex[:4]}")
    session.add(project)
    session.flush()
    task = Task(workspace_id=ws.id, project_id=project.id, key="T-1", title="line")
    session.add(task)
    session.flush()
    run = WorkflowRun(workspace_id=ws.id, task_id=task.id, current_state=state)
    session.add(run)
    session.flush()
    return ws.id, run.id


def _visit(ws_id: uuid.UUID, run_id: uuid.UUID, **overrides: Any) -> WorkflowStationVisit:
    values: dict[str, Any] = {
        "workspace_id": ws_id,
        "workflow_run_id": run_id,
        "definition_revision_id": uuid.uuid4(),
        "state": "implement",
        "visit": 1,
        "kind": "agent",
        "status": "running",
        "started_at": datetime.now(UTC),
    }
    values.update(overrides)
    return WorkflowStationVisit(**values)


# --- model (SQLite) ---------------------------------------------------------- #


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    engine.dispose()


def test_station_visit_round_trip_and_defaults(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id, run_id = _seed_run(session, "implement")
        visit = _visit(ws_id, run_id)
        session.add(visit)
        session.commit()
        visit_id = visit.id
    with factory() as session:
        loaded = session.get(WorkflowStationVisit, visit_id)
        assert loaded is not None
        assert (loaded.outputs, loaded.tokens_used, loaded.priced) == ({}, 0, False)
        assert loaded.cost_usd is None and loaded.finished_at is None
        assert session.get(WorkflowRun, run_id).current_state == "implement"  # type: ignore[union-attr]


def test_station_visit_unique_per_run_state_visit(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id, run_id = _seed_run(session, "implement")
        session.add(_visit(ws_id, run_id))
        session.commit()
        session.add(_visit(ws_id, run_id))
        with pytest.raises(IntegrityError):
            session.commit()


@pytest.mark.parametrize("bad", [{"kind": "robot"}, {"status": "done"}])
def test_station_visit_checks(factory: sessionmaker[Session], bad: dict[str, str]) -> None:
    with factory() as session:
        ws_id, run_id = _seed_run(session, "implement")
        session.add(_visit(ws_id, run_id, **bad))
        with pytest.raises(IntegrityError):
            session.commit()


# --- migration chain (SQLite via Alembic) ------------------------------------ #


def test_migration_0045_chain_on_sqlite(tmp_path: Path) -> None:
    cfg = Config(str(DB_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(DB_ROOT / "migrations"))
    url = f"sqlite:///{tmp_path / 'forge_test.db'}"
    cfg.set_main_option("sqlalchemy.url", url)
    engine = create_engine(url)

    def state() -> tuple[bool, list[str]]:
        inspector = inspect(engine)
        keys = {
            uc["name"]: uc["column_names"]
            for uc in inspector.get_unique_constraints("marketplace_installation")
        }
        has_visit = "workflow_station_visit" in inspector.get_table_names()
        return has_visit, keys["uq_marketplace_installation_pkg"]

    try:
        command.upgrade(cfg, "head")
        assert state() == (True, NEW_KEY)
        command.downgrade(cfg, "0044_agent_run_context_refs")
        assert state() == (False, OLD_KEY)
        command.upgrade(cfg, "head")
        assert state() == (True, NEW_KEY)
    finally:
        engine.dispose()


# --- migration 0045 on Postgres ---------------------------------------------- #


def _current_state_length(engine: Engine) -> int | None:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT character_maximum_length FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'workflow_run' "
                "AND column_name = 'current_state'"
            )
        ).scalar_one()


def _install_key(engine: Engine) -> list[str]:
    keys = {
        uc["name"]: uc["column_names"]
        for uc in inspect(engine).get_unique_constraints("marketplace_installation")
    }
    return keys["uq_marketplace_installation_pkg"]


@pytest.fixture
def pg_schema(pg_engine: Engine) -> Iterator[Engine]:
    Base.metadata.create_all(pg_engine)
    try:
        yield pg_engine
    finally:
        Base.metadata.drop_all(pg_engine)


def test_migration_0045_down_and_up_on_postgres(pg_schema: Engine) -> None:
    module = _load_migration()
    assert module.revision == "0045_workflow_lines"
    assert module.down_revision == "0044_agent_run_context_refs"

    _run(pg_schema, "downgrade")
    inspector = inspect(pg_schema)
    assert "workflow_station_visit" not in inspector.get_table_names()
    assert _current_state_length(pg_schema) is None  # 0044: unbounded VARCHAR
    assert _install_key(pg_schema) == OLD_KEY

    _run(pg_schema, "upgrade")
    inspector = inspect(pg_schema)
    assert _current_state_length(pg_schema) == 64
    assert not [
        c
        for c in inspector.get_check_constraints("workflow_run")
        if "current_state" in c["sqltext"]
    ]
    assert _install_key(pg_schema) == NEW_KEY

    # The station-visit table: unique key, CHECKs, index and the cascading FK.
    table = "workflow_station_visit"
    uniques = {tuple(uc["column_names"]) for uc in inspector.get_unique_constraints(table)}
    assert ("workflow_run_id", "state", "visit") in uniques
    checks = {c["name"]: c["sqltext"] for c in inspector.get_check_constraints(table)}
    assert "'human'" in checks["ck_workflow_station_visit_workflow_station_visit_kind"]
    assert "'parked'" in checks["ck_workflow_station_visit_workflow_station_visit_status"]
    indexes = {ix["name"]: ix["column_names"] for ix in inspector.get_indexes(table)}
    assert indexes["ix_workflow_station_visit_run"] == ["workspace_id", "workflow_run_id"]
    (run_fk,) = [
        fk for fk in inspector.get_foreign_keys(table) if fk["referred_table"] == "workflow_run"
    ]
    assert run_fk["constrained_columns"] == ["workflow_run_id"]
    assert run_fk["options"]["ondelete"] == "CASCADE"
    columns = {c["name"]: c for c in inspector.get_columns(table)}
    assert str(columns["cost_usd"]["type"]) == "NUMERIC(12, 4)"
    assert columns["cost_usd"]["nullable"] is True
    assert str(columns["state"]["type"]) == "VARCHAR(64)"
    assert str(columns["outcome"]["type"]) == "VARCHAR(48)"


def test_migration_0045_downgrade_refuses_line_rows_on_postgres(pg_schema: Engine) -> None:
    factory = sessionmaker(bind=pg_schema, expire_on_commit=False, class_=Session)
    with factory() as session:
        ws_id, run_id = _seed_run(session, "review_changes")
        session.add(_visit(ws_id, run_id))
        session.commit()

    with pytest.raises(RuntimeError, match=r"1 workflow_run row\(s\) hold a state outside"):
        _run(pg_schema, "downgrade")
    # Refused before any change.
    assert "workflow_station_visit" in inspect(pg_schema).get_table_names()
    assert _current_state_length(pg_schema) == 64

    with factory() as session:
        session.get(WorkflowRun, run_id).current_state = "created"  # type: ignore[union-attr]
        session.add(
            WorkflowDefinition(
                workspace_id=ws_id,
                name="impl-loop",
                title="Implementation loop",
                source=WorkflowDefinitionSource.MARKETPLACE,
            )
        )
        session.commit()
    with pytest.raises(RuntimeError, match=r"1 workflow_definition row\(s\) came from"):
        _run(pg_schema, "downgrade")

    with factory() as session:
        session.execute(text("DELETE FROM workflow_definition"))
        for kind in ("mcp_connector", "skill_profile"):
            session.add(
                MarketplaceInstallation(
                    workspace_id=ws_id,
                    registry_slug="official",
                    listing_slug="shape-shift",
                    kind=kind,
                    installed_version="1.0.0",
                    target_kind=kind,
                    content_hash="sha256:" + "0" * 64,
                    verification_status="verified",
                )
            )
        session.commit()
    with pytest.raises(RuntimeError, match=r"1 marketplace_installation slug\(s\) are installed"):
        _run(pg_schema, "downgrade")
    assert "workflow_station_visit" in inspect(pg_schema).get_table_names()

    with factory() as session:
        session.execute(text("DELETE FROM marketplace_installation WHERE kind = 'skill_profile'"))
        session.commit()
    _run(pg_schema, "downgrade")
    assert "workflow_station_visit" not in inspect(pg_schema).get_table_names()
    _run(pg_schema, "upgrade")  # leave the schema whole for drop_all
