"""Migration 0055: the ``held_automation_action`` table (#191)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

DB_ROOT = Path(__file__).resolve().parent.parent
REVISION = "0055_held_automation_action"
TABLE = "held_automation_action"
COLUMNS = {
    "id",
    "workspace_id",
    "approval_request_id",
    "action",
    "context",
    "released_at",
    "released_by",
    "created_at",
    "updated_at",
}


def test_migration_0055_creates_and_drops_the_held_action_table(tmp_path: Path) -> None:
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
        assert {c["name"] for c in inspector.get_columns(TABLE)} == COLUMNS
        fks = {fk["referred_table"] for fk in inspector.get_foreign_keys(TABLE)}
        assert {"workspace", "approval_request"} <= fks
        unique = [c["column_names"] for c in inspector.get_unique_constraints(TABLE)]
        assert ["approval_request_id"] in unique  # one held action per approval request

        command.downgrade(cfg, previous)
        assert TABLE not in inspect(engine).get_table_names()

        command.upgrade(cfg, "head")
        assert TABLE in inspect(engine).get_table_names()
    finally:
        engine.dispose()
