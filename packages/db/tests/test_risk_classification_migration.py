"""Migration 0053: the ``risk_classification`` and ``risk_gate_observation`` tables (#193)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

DB_ROOT = Path(__file__).resolve().parent.parent
REVISION = "0053_risk_classification"
#: Spec A §5.3, both workspace-scoped.
COLUMNS = {
    "risk_classification": {
        "id",
        "workspace_id",
        "commit_sha",
        "pr_numbers",
        "agent_run_id",
        "workflow_run_id",
        "spec_key",
        "spec_version",
        "derived_class",
        "declared_class",
        "final_class",
        "matched_rule_ids",
        "signals",
        "override_approver",
        "override_reason",
        "enforcement_mode",
        "fallback_applied",
        "created_at",
    },
    "risk_gate_observation": {
        "id",
        "workspace_id",
        "classification_id",
        "gate_name",
        "actual_decision",
        "risk_decision",
        "agreed",
        "created_at",
    },
}


def test_migration_0053_creates_and_drops_the_risk_tables(tmp_path: Path) -> None:
    cfg = Config(str(DB_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(DB_ROOT / "migrations"))
    url = f"sqlite:///{tmp_path / 'scf_test.db'}"
    cfg.set_main_option("sqlalchemy.url", url)
    previous = ScriptDirectory.from_config(cfg).get_revision(REVISION).down_revision
    assert isinstance(previous, str)
    engine = create_engine(url)
    try:
        command.upgrade(cfg, "head")
        inspector = inspect(engine)
        for table, columns in COLUMNS.items():
            assert columns <= {c["name"] for c in inspector.get_columns(table)}
            fks = {fk["referred_table"] for fk in inspector.get_foreign_keys(table)}
            assert "workspace" in fks
        observation_fks = inspector.get_foreign_keys("risk_gate_observation")
        assert {"risk_classification"} <= {fk["referred_table"] for fk in observation_fks}

        command.downgrade(cfg, previous)
        assert not set(COLUMNS) & set(inspect(engine).get_table_names())

        command.upgrade(cfg, "head")
        assert set(COLUMNS) <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
