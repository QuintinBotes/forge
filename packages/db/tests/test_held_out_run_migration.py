"""Migration 0051: ``held_out_scenario.workflow_run_id`` (#288), reversible."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

DB_ROOT = Path(__file__).resolve().parent.parent
TABLE = "held_out_scenario"


def _shape(url: str) -> tuple[set[str], set[str]]:
    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        columns = {c["name"] for c in inspector.get_columns(TABLE)}
        indexes = {i["name"] for i in inspector.get_indexes(TABLE)}
        return columns, indexes
    finally:
        engine.dispose()


def test_migration_0051_adds_and_drops_the_scenarios_run(tmp_path: Path) -> None:
    cfg = Config(str(DB_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(DB_ROOT / "migrations"))
    url = f"sqlite:///{tmp_path / 'scf_test.db'}"
    cfg.set_main_option("sqlalchemy.url", url)

    command.upgrade(cfg, "head")
    columns, indexes = _shape(url)
    assert "workflow_run_id" in columns
    assert "ix_held_out_scenario_workflow_run_id" in indexes

    command.downgrade(cfg, "0050_sign_in_link")
    columns, indexes = _shape(url)
    assert "workflow_run_id" not in columns and "title" in columns
    assert "ix_held_out_scenario_workflow_run_id" not in indexes

    command.upgrade(cfg, "head")
    assert "workflow_run_id" in _shape(url)[0]
