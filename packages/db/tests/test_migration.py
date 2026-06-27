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


# F19 container-sandboxing table, owned by the 0003_container_sandboxing migration.
SANDBOX_TABLES = {"sandbox_instance"}


def test_sandbox_migration_up_down(alembic_config: Config) -> None:
    """F19 AC: 0003_container_sandboxing creates the sandbox_instance table and
    drops it on downgrade, independently of the baseline + PM tables."""
    url = alembic_config.get_main_option("sqlalchemy.url")
    assert url is not None
    engine = create_engine(url)
    try:
        # Up to PM migration: sandbox table absent, core present.
        command.upgrade(alembic_config, "0002_pm_adapters")
        after_pm = set(inspect(engine).get_table_names())
        assert not (SANDBOX_TABLES & after_pm), "PM stage must not create sandbox tables"
        assert after_pm >= EXPECTED_TABLES

        # Apply the sandbox migration: the table appears.
        command.upgrade(alembic_config, "0003_container_sandboxing")
        after_sbx = set(inspect(engine).get_table_names())
        assert after_sbx >= SANDBOX_TABLES, "missing sandbox_instance table"

        # Downgrade one step: sandbox table gone, core + PM untouched.
        command.downgrade(alembic_config, "0002_pm_adapters")
        after_down = set(inspect(engine).get_table_names())
        assert not (SANDBOX_TABLES & after_down), "downgrade left sandbox tables"
        assert after_down >= EXPECTED_TABLES

        command.downgrade(alembic_config, "base")
    finally:
        engine.dispose()


# F20 MCP sync-and-index tables, owned by the 0004_mcp_sync_and_index migration.
F20_TABLES = {"knowledge_sync_run", "mcp_indexed_resource"}


def test_mcp_sync_index_migration_up_down(alembic_config: Config) -> None:
    """F20 AC1: 0004_mcp_sync_and_index creates the knowledge_sync_run +
    mcp_indexed_resource tables and drops them on downgrade, leaving the
    baseline + PM + sandbox tables intact."""
    url = alembic_config.get_main_option("sqlalchemy.url")
    assert url is not None
    engine = create_engine(url)
    try:
        # Up to the sandbox migration: F20 tables absent, core present.
        command.upgrade(alembic_config, "0003_container_sandboxing")
        after_sbx = set(inspect(engine).get_table_names())
        assert not (F20_TABLES & after_sbx), "sandbox stage must not create F20 tables"
        assert after_sbx >= EXPECTED_TABLES

        # Apply the F20 migration: both tables appear with the documented indexes.
        command.upgrade(alembic_config, "0004_mcp_sync_and_index")
        inspector = inspect(engine)
        after_f20 = set(inspector.get_table_names())
        assert after_f20 >= F20_TABLES, f"missing F20 tables: {sorted(F20_TABLES - after_f20)}"

        index_names = {ix["name"] for ix in inspector.get_indexes("mcp_indexed_resource")}
        assert "ux_mcp_idx_resource" in index_names
        assert "ix_mcp_idx_tenant_source" in index_names
        assert "ix_mcp_idx_seen" in index_names
        unique_indexes = {
            ix["name"] for ix in inspector.get_indexes("mcp_indexed_resource") if ix["unique"]
        }
        assert "ux_mcp_idx_resource" in unique_indexes

        # Downgrade one step: F20 tables gone, core + PM + sandbox untouched.
        command.downgrade(alembic_config, "0003_container_sandboxing")
        after_down = set(inspect(engine).get_table_names())
        assert not (F20_TABLES & after_down), "downgrade left F20 tables"
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
