"""Migration 0057: ``change_outcome.merge_commit_sha`` and its index (#335)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

DB_ROOT = Path(__file__).resolve().parent.parent


def _schema(url: str) -> tuple[set[str], set[str]]:
    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        columns = {c["name"] for c in inspector.get_columns("change_outcome")}
        indexes = {str(i["name"]) for i in inspector.get_indexes("change_outcome")}
        return columns, indexes
    finally:
        engine.dispose()


def test_migration_0057_adds_and_drops_the_merge_commit(tmp_path: Path) -> None:
    cfg = Config(str(DB_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(DB_ROOT / "migrations"))
    url = f"sqlite:///{tmp_path / 'scf_test.db'}"
    cfg.set_main_option("sqlalchemy.url", url)

    command.upgrade(cfg, "0057_change_outcome_merge_commit")
    columns, indexes = _schema(url)
    assert "merge_commit_sha" in columns and "ix_change_outcome_merge_commit" in indexes

    command.downgrade(cfg, "0055_risk_class_requirements")
    columns, indexes = _schema(url)
    assert "merge_commit_sha" not in columns and "ix_change_outcome_merge_commit" not in indexes
    assert "outcome" in columns  # the table itself stays

    command.upgrade(cfg, "0057_change_outcome_merge_commit")
    columns, indexes = _schema(url)
    assert "merge_commit_sha" in columns and "ix_change_outcome_merge_commit" in indexes
