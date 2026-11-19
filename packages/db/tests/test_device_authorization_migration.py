"""Migration 0052: the ``device_authorization`` table (#207), reversible."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

DB_ROOT = Path(__file__).resolve().parent.parent
TABLE = "device_authorization"


def test_migration_0052_creates_and_drops_the_device_authorization_table(tmp_path: Path) -> None:
    cfg = Config(str(DB_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(DB_ROOT / "migrations"))
    url = f"sqlite:///{tmp_path / 'scf_test.db'}"
    cfg.set_main_option("sqlalchemy.url", url)
    engine = create_engine(url)
    try:
        command.upgrade(cfg, "head")
        inspector = inspect(engine)
        assert TABLE in inspector.get_table_names()
        columns = {c["name"]: c for c in inspector.get_columns(TABLE)}
        assert {
            "device_code_hash",
            "user_code_selector",
            "user_code_hash",
            "client_name",
            "status",
            "expires_at",
            "last_polled_at",
            "failed_attempts",
            "requester_address",
            "requester_agent",
        } <= columns.keys()
        assert not {"device_code", "user_code", "token"} & columns.keys()
        # The workspace and the person are unknown until someone approves.
        assert columns["workspace_id"]["nullable"] and columns["user_id"]["nullable"]
        uniques = [u["column_names"] for u in inspector.get_unique_constraints(TABLE)]
        indexes = [i["column_names"] for i in inspector.get_indexes(TABLE) if i["unique"]]
        assert ["device_code_hash"] in uniques + indexes
        # A pending request's user code is unique, so an approval names one request.
        assert ["user_code_hash"] in indexes

        command.downgrade(cfg, "0051_held_out_scenario_run")
        assert TABLE not in inspect(engine).get_table_names()

        command.upgrade(cfg, "head")
        assert TABLE in inspect(engine).get_table_names()
    finally:
        engine.dispose()
