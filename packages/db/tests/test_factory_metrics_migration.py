"""Migration 0054: ``change_outcome`` and ``run_intervention`` (#196), reversible."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

DB_ROOT = Path(__file__).resolve().parent.parent
TABLES = {"change_outcome", "run_intervention"}


def test_migration_0054_creates_and_drops_the_factory_metrics_tables(tmp_path: Path) -> None:
    cfg = Config(str(DB_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(DB_ROOT / "migrations"))
    url = f"sqlite:///{tmp_path / 'scf_test.db'}"
    cfg.set_main_option("sqlalchemy.url", url)
    engine = create_engine(url)
    try:
        command.upgrade(cfg, "head")
        inspector = inspect(engine)
        assert set(inspector.get_table_names()) >= TABLES
        for table in TABLES:
            assert "workspace_id" in {c["name"] for c in inspector.get_columns(table)}
        # One outcome per attempt; one intervention per run, kind and source.
        outcome_uniques = [
            u["column_names"] for u in inspector.get_unique_constraints("change_outcome")
        ] + [i["column_names"] for i in inspector.get_indexes("change_outcome") if i["unique"]]
        assert ["agent_run_id"] in outcome_uniques
        assert ["agent_run_id", "kind", "source_key"] in [
            i["column_names"] for i in inspector.get_indexes("run_intervention") if i["unique"]
        ]

        command.downgrade(cfg, "0053_risk_classification")
        assert not TABLES & set(inspect(engine).get_table_names())

        command.upgrade(cfg, "head")
        assert set(inspect(engine).get_table_names()) >= TABLES
    finally:
        engine.dispose()
