"""Migration 0050: the ``sign_in_link`` table (#260), reversible."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

DB_ROOT = Path(__file__).resolve().parent.parent
TABLE = "sign_in_link"


def test_migration_0050_creates_and_drops_the_sign_in_link_table(tmp_path: Path) -> None:
    cfg = Config(str(DB_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(DB_ROOT / "migrations"))
    url = f"sqlite:///{tmp_path / 'scf_test.db'}"
    cfg.set_main_option("sqlalchemy.url", url)
    engine = create_engine(url)
    try:
        command.upgrade(cfg, "head")
        inspector = inspect(engine)
        assert TABLE in inspector.get_table_names()
        columns = {c["name"] for c in inspector.get_columns(TABLE)}
        assert {"workspace_id", "user_id", "token_hash", "expires_at", "used_at"} <= columns
        assert "token" not in columns
        uniques = [u["column_names"] for u in inspector.get_unique_constraints(TABLE)]
        indexes = [i["column_names"] for i in inspector.get_indexes(TABLE) if i["unique"]]
        assert ["token_hash"] in uniques + indexes

        command.downgrade(cfg, "0049_held_out_scenarios")
        assert TABLE not in inspect(engine).get_table_names()

        command.upgrade(cfg, "head")
        assert TABLE in inspect(engine).get_table_names()
    finally:
        engine.dispose()
