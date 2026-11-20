"""Migration 0055: ``risk_classification.class_requirements`` (#194)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

DB_ROOT = Path(__file__).resolve().parent.parent


def _columns(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return {c["name"] for c in inspect(engine).get_columns("risk_classification")}
    finally:
        engine.dispose()


def test_migration_0055_adds_and_drops_the_class_requirements(tmp_path: Path) -> None:
    cfg = Config(str(DB_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(DB_ROOT / "migrations"))
    url = f"sqlite:///{tmp_path / 'scf_test.db'}"
    cfg.set_main_option("sqlalchemy.url", url)

    command.upgrade(cfg, "0055_risk_class_requirements")
    assert "class_requirements" in _columns(url)

    command.downgrade(cfg, "0053_risk_classification")
    assert "class_requirements" not in _columns(url)
    assert "final_class" in _columns(url)  # the table itself stays

    command.upgrade(cfg, "0055_risk_class_requirements")
    assert "class_requirements" in _columns(url)
