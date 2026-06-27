"""Alembic baseline migration tests (Task 0.2).

The baseline migration is applied against SQLite here (a real
``alembic upgrade head`` round-trip exercising ``env.py`` + the version
script). Applying against a live Postgres container is PARKED — no Postgres is
reachable in the unit sandbox — but the Postgres column types are independently
verified in ``test_models.py`` via dialect compilation, and the same migration
code path runs here on SQLite.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

import forge_db.models as models

DB_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = DB_ROOT / "alembic.ini"

EXPECTED_TABLES = {
    getattr(models, n).__tablename__
    for n in [
        "Workspace",
        "User",
        "APIKey",
        "RepositoryConnection",
        "MCPConnection",
        "PolicyProfile",
        "SkillProfile",
        "KnowledgeSource",
        "RetrievalChunk",
        "Project",
        "Constitution",
        "Epic",
        "SpecDocument",
        "Task",
        "Incident",
        "Sprint",
        "Milestone",
        "WorkflowRun",
        "AgentRun",
        "ApprovalRequest",
        "SubAgentRun",
        "IncidentAlert",
        "IncidentEvent",
        "RemediationPlan",
        "Postmortem",
        "PostmortemActionItem",
    ]
}


@pytest.fixture
def alembic_config(tmp_path) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(DB_ROOT / "migrations"))
    db_file = tmp_path / "forge_test.db"
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")
    return cfg


def test_alembic_ini_exists() -> None:
    assert ALEMBIC_INI.is_file()


def test_single_baseline_revision(alembic_config: Config) -> None:
    script = ScriptDirectory.from_config(alembic_config)
    bases = script.get_bases()
    assert len(bases) == 1, "expected exactly one baseline revision"
    heads = script.get_heads()
    assert len(heads) == 1, "expected a single linear head"


def test_upgrade_then_downgrade_roundtrip(alembic_config: Config) -> None:
    url = alembic_config.get_main_option("sqlalchemy.url")
    assert url is not None

    command.upgrade(alembic_config, "head")
    engine = create_engine(url)
    try:
        tables = set(inspect(engine).get_table_names())
        missing = EXPECTED_TABLES - tables
        assert not missing, f"migration did not create: {sorted(missing)}"

        command.downgrade(alembic_config, "base")
        remaining = set(inspect(engine).get_table_names()) & EXPECTED_TABLES
        assert not remaining, f"downgrade left tables: {sorted(remaining)}"
    finally:
        engine.dispose()


# F18 external PM-adapter tables, owned by the 0002_pm_adapters migration.
PM_TABLES = {"pm_connection", "pm_task_link", "pm_webhook_delivery"}


def test_pm_adapters_migration_up_down(alembic_config: Config) -> None:
    """AC1: 0002_pm_adapters creates the three PM tables and drops them on
    downgrade, independently of the baseline core tables."""
    url = alembic_config.get_main_option("sqlalchemy.url")
    assert url is not None
    engine = create_engine(url)
    try:
        # Baseline only: PM tables absent, core present.
        command.upgrade(alembic_config, "0001_baseline")
        after_baseline = set(inspect(engine).get_table_names())
        assert not (PM_TABLES & after_baseline), "baseline must not create PM tables"
        assert after_baseline >= EXPECTED_TABLES

        # Apply the PM migration: all three PM tables appear.
        command.upgrade(alembic_config, "0002_pm_adapters")
        after_pm = set(inspect(engine).get_table_names())
        assert after_pm >= PM_TABLES, f"missing PM tables: {sorted(PM_TABLES - after_pm)}"

        # Downgrade one step: PM tables gone, core untouched.
        command.downgrade(alembic_config, "0001_baseline")
        after_down = set(inspect(engine).get_table_names())
        assert not (PM_TABLES & after_down), "downgrade left PM tables"
        assert after_down >= EXPECTED_TABLES

        command.downgrade(alembic_config, "base")
    finally:
        engine.dispose()


# PARKED: applying the baseline migration against a live Postgres (pgvector)
# container is not runnable in the unit sandbox (no Postgres reachable / no
# network). The identical migration code path is exercised above on SQLite, and
# the Postgres-specific column types (VECTOR(1536), TSVECTOR, JSONB) plus the
# CREATE EXTENSION vector step are verified via dialect compilation in
# test_models.py. Re-run `alembic -c packages/db/alembic.ini upgrade head` with
# FORGE_DATABASE_URL pointing at Postgres in Phase 2 (docker compose) to close.
