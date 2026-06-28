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


# F21 automation tables, owned by the 0005_f21_automations migration.
F21_TABLES = {"automation_rule", "automation_execution"}


def test_f21_automations_migration_up_down(alembic_config: Config) -> None:
    """F21 AC: 0005_f21_automations creates the automation tables and drops them
    on downgrade, leaving the baseline + PM + sandbox + F20 tables intact."""
    url = alembic_config.get_main_option("sqlalchemy.url")
    assert url is not None
    engine = create_engine(url)
    try:
        # Up to the F20 migration: F21 tables absent, core present.
        command.upgrade(alembic_config, "0004_mcp_sync_and_index")
        after_f20 = set(inspect(engine).get_table_names())
        assert not (F21_TABLES & after_f20), "F20 stage must not create F21 tables"
        assert after_f20 >= EXPECTED_TABLES

        # Apply the F21 migration: both tables appear with the dispatch index.
        command.upgrade(alembic_config, "0005_f21_automations")
        inspector = inspect(engine)
        after_f21 = set(inspector.get_table_names())
        assert after_f21 >= F21_TABLES, f"missing F21 tables: {sorted(F21_TABLES - after_f21)}"

        exec_uniques = {
            uc["name"] for uc in inspector.get_unique_constraints("automation_execution")
        }
        assert "uq_automation_execution_idempotency_key" in exec_uniques

        # Downgrade one step: F21 tables gone, everything else untouched.
        command.downgrade(alembic_config, "0004_mcp_sync_and_index")
        after_down = set(inspect(engine).get_table_names())
        assert not (F21_TABLES & after_down), "downgrade left F21 tables"
        assert after_down >= EXPECTED_TABLES

        command.downgrade(alembic_config, "base")
    finally:
        engine.dispose()


# F22 multi-repo tables, owned by the 0006_f22_multi_repo migration.
F22_TABLES = {"pr_group", "agent_repo_workspace"}


def test_f22_multi_repo_migration_up_down(alembic_config: Config) -> None:
    """F22 AC: 0006_f22_multi_repo creates the pr_group + agent_repo_workspace
    tables (with their unique constraints) and drops them on downgrade, leaving
    the baseline + PM + sandbox + F20 + F21 tables intact."""
    url = alembic_config.get_main_option("sqlalchemy.url")
    assert url is not None
    engine = create_engine(url)
    try:
        # Up to the F21 migration: F22 tables absent, core present.
        command.upgrade(alembic_config, "0005_f21_automations")
        after_f21 = set(inspect(engine).get_table_names())
        assert not (F22_TABLES & after_f21), "F21 stage must not create F22 tables"
        assert after_f21 >= EXPECTED_TABLES

        # Apply the F22 migration: both tables appear with their unique constraints.
        command.upgrade(alembic_config, "0006_f22_multi_repo")
        inspector = inspect(engine)
        after_f22 = set(inspector.get_table_names())
        assert after_f22 >= F22_TABLES, f"missing F22 tables: {sorted(F22_TABLES - after_f22)}"

        pr_group_uniques = {
            uc["name"] for uc in inspector.get_unique_constraints("pr_group")
        }
        assert "uq_pr_group_workflow_run_id" in pr_group_uniques
        arw_uniques = {
            uc["name"] for uc in inspector.get_unique_constraints("agent_repo_workspace")
        }
        assert "uq_agent_repo_workspace_agent_run_id_repo_id" in arw_uniques

        # Downgrade one step: F22 tables gone, everything else untouched.
        command.downgrade(alembic_config, "0005_f21_automations")
        after_down = set(inspect(engine).get_table_names())
        assert not (F22_TABLES & after_down), "downgrade left F22 tables"
        assert after_down >= EXPECTED_TABLES

        command.downgrade(alembic_config, "base")
    finally:
        engine.dispose()


# F25 temporal-engine columns on workflow_run, owned by 0007_f25_temporal_engine.
F25_COLUMNS = {"engine_backend", "temporal_workflow_id", "temporal_run_id"}
F25_INDEXES = {"ix_workflow_run_temporal_wfid", "uq_workflow_run_temporal_workflow_id"}


def test_f25_temporal_engine_migration_up_down(alembic_config: Config) -> None:
    """F25 AC17: 0007 adds the engine-attribution columns + indexes to
    workflow_run and drops them on downgrade, leaving the table intact.

    (forge_db's baseline is metadata-driven, so the columns are provisioned on a
    fresh chain; this asserts the F25 migration *owns* a clean, reversible down.)"""
    url = alembic_config.get_main_option("sqlalchemy.url")
    assert url is not None
    engine = create_engine(url)
    try:
        command.upgrade(alembic_config, "head")
        inspector = inspect(engine)
        cols = {c["name"] for c in inspector.get_columns("workflow_run")}
        assert cols >= F25_COLUMNS, f"missing F25 columns: {sorted(F25_COLUMNS - cols)}"
        idx = {i["name"] for i in inspector.get_indexes("workflow_run")}
        assert idx >= F25_INDEXES, f"missing F25 indexes: {sorted(F25_INDEXES - idx)}"
        unique = {i["name"] for i in inspector.get_indexes("workflow_run") if i["unique"]}
        assert "uq_workflow_run_temporal_workflow_id" in unique

        # Downgrade one step: F25 columns/indexes gone, workflow_run still present.
        command.downgrade(alembic_config, "0006_f22_multi_repo")
        inspector = inspect(engine)
        assert "workflow_run" in inspector.get_table_names()
        cols_after = {c["name"] for c in inspector.get_columns("workflow_run")}
        assert not (F25_COLUMNS & cols_after), "downgrade left F25 columns"

        command.downgrade(alembic_config, "base")
    finally:
        engine.dispose()


# F26 sprint-velocity tables + sprint columns, owned by 0008_f26_sprint_velocity.
F26_TABLES = {"sprint_scope_event", "sprint_burndown_snapshot", "sprint_velocity"}
F26_SPRINT_COLUMNS = {
    "started_at",
    "completed_at",
    "capacity_points",
    "committed_points",
    "committed_task_count",
    "committed_task_ids",
    "position",
    "velocity_version",
}


def test_f26_sprint_velocity_migration_up_down(alembic_config: Config) -> None:
    """F26 AC: 0008 adds the sprint lifecycle columns + partial unique index and
    the three sprint-velocity tables, and drops them on downgrade, leaving the
    baseline ``sprint`` table intact."""
    url = alembic_config.get_main_option("sqlalchemy.url")
    assert url is not None
    engine = create_engine(url)
    try:
        # Up to F25: F26 tables absent, core present.
        command.upgrade(alembic_config, "0007_f25_temporal_engine")
        after_f25 = set(inspect(engine).get_table_names())
        assert not (F26_TABLES & after_f25), "F25 stage must not create F26 tables"
        assert after_f25 >= EXPECTED_TABLES

        # Apply the F26 migration: tables + columns + index appear.
        command.upgrade(alembic_config, "0008_f26_sprint_velocity")
        inspector = inspect(engine)
        after_f26 = set(inspector.get_table_names())
        assert after_f26 >= F26_TABLES, f"missing F26 tables: {sorted(F26_TABLES - after_f26)}"

        sprint_cols = {c["name"] for c in inspector.get_columns("sprint")}
        assert sprint_cols >= F26_SPRINT_COLUMNS, (
            f"missing sprint columns: {sorted(F26_SPRINT_COLUMNS - sprint_cols)}"
        )
        sprint_indexes = {i["name"] for i in inspector.get_indexes("sprint")}
        assert "uq_active_sprint_per_project" in sprint_indexes

        velocity_uniques = {
            uc["name"] for uc in inspector.get_unique_constraints("sprint_velocity")
        }
        assert "uq_sprint_velocity_sprint_id" in velocity_uniques

        # Downgrade one step: F26 tables + columns gone, sprint table intact.
        command.downgrade(alembic_config, "0007_f25_temporal_engine")
        inspector = inspect(engine)
        after_down = set(inspector.get_table_names())
        assert not (F26_TABLES & after_down), "downgrade left F26 tables"
        assert "sprint" in after_down
        cols_after = {c["name"] for c in inspector.get_columns("sprint")}
        assert not (F26_SPRINT_COLUMNS & cols_after), "downgrade left F26 sprint columns"

        command.downgrade(alembic_config, "base")
    finally:
        engine.dispose()


# F27 supervised multi-agent columns, owned by 0009_f27_multi_agent.
F27_AGENT_RUN_COLUMNS = {"is_supervisor", "pattern", "supervision"}
F27_SUB_AGENT_RUN_COLUMNS = {
    "agent_run_id",
    "assignment_id",
    "pattern",
    "ordinal",
    "depends_on",
    "optional",
    "objective",
    "artifact",
    "branch_name",
    "merged",
    "token_usage",
    "error",
}


def test_f27_multi_agent_migration_up_down(alembic_config: Config) -> None:
    """F27 AC: 0009 owns the coordinator columns on agent_run + sub_agent_run
    (with their indexes + unique index) and drops them on downgrade, leaving the
    baseline-owned tables intact.

    (forge_db's baseline is metadata-driven, so the columns are provisioned on a
    fresh chain; this asserts the F27 migration owns a clean, reversible down.)"""
    url = alembic_config.get_main_option("sqlalchemy.url")
    assert url is not None
    engine = create_engine(url)
    try:
        command.upgrade(alembic_config, "head")
        inspector = inspect(engine)
        agent_cols = {c["name"] for c in inspector.get_columns("agent_run")}
        sub_cols = {c["name"] for c in inspector.get_columns("sub_agent_run")}
        assert agent_cols >= F27_AGENT_RUN_COLUMNS
        assert sub_cols >= F27_SUB_AGENT_RUN_COLUMNS
        sub_indexes = {i["name"] for i in inspector.get_indexes("sub_agent_run")}
        assert {"ix_sub_agent_run_parent", "ix_sub_agent_run_child"} <= sub_indexes
        unique_indexes = {i["name"] for i in inspector.get_indexes("sub_agent_run") if i["unique"]}
        assert "uq_sub_agent_run_assignment" in unique_indexes

        # Downgrade one step: F27 columns/indexes gone, tables intact.
        command.downgrade(alembic_config, "0008_f26_sprint_velocity")
        inspector = inspect(engine)
        assert "agent_run" in inspector.get_table_names()
        assert "sub_agent_run" in inspector.get_table_names()
        agent_cols = {c["name"] for c in inspector.get_columns("agent_run")}
        sub_cols = {c["name"] for c in inspector.get_columns("sub_agent_run")}
        assert not (F27_AGENT_RUN_COLUMNS & agent_cols), "downgrade left F27 agent_run cols"
        assert not (F27_SUB_AGENT_RUN_COLUMNS & sub_cols), "downgrade left F27 sub_agent_run cols"

        command.downgrade(alembic_config, "base")
    finally:
        engine.dispose()


# F28 workflow-editor tables + run pin column, owned by 0010_f28_workflow_editor.
F28_TABLES = {"workflow_definition", "workflow_definition_revision"}
F28_RUN_COLUMNS = {"definition_revision_id"}


def test_f28_workflow_editor_migration_up_down(alembic_config: Config) -> None:
    """F28 AC: 0010 creates the two editor tables (with the single-draft partial
    unique index) + the workflow_run pin column, and drops them on downgrade,
    leaving workflow_run intact."""
    url = alembic_config.get_main_option("sqlalchemy.url")
    assert url is not None
    engine = create_engine(url)
    try:
        # Up to F27: F28 tables absent, core present.
        command.upgrade(alembic_config, "0009_f27_multi_agent")
        after_f27 = set(inspect(engine).get_table_names())
        assert not (F28_TABLES & after_f27), "F27 stage must not create F28 tables"
        assert after_f27 >= EXPECTED_TABLES

        # Apply the F28 migration: both tables + the pin column appear.
        command.upgrade(alembic_config, "0010_f28_workflow_editor")
        inspector = inspect(engine)
        after_f28 = set(inspector.get_table_names())
        assert after_f28 >= F28_TABLES, f"missing F28 tables: {sorted(F28_TABLES - after_f28)}"

        rev_indexes = {i["name"] for i in inspector.get_indexes("workflow_definition_revision")}
        assert "uq_workflow_definition_revision_one_draft" in rev_indexes
        assert "uq_workflow_definition_revision_revision" in rev_indexes
        def_indexes = {i["name"] for i in inspector.get_indexes("workflow_definition")}
        assert "uq_workflow_definition_workspace_name" in def_indexes

        run_cols = {c["name"] for c in inspector.get_columns("workflow_run")}
        assert run_cols >= F28_RUN_COLUMNS

        # Downgrade one step: F28 tables + column gone, workflow_run intact.
        command.downgrade(alembic_config, "0009_f27_multi_agent")
        inspector = inspect(engine)
        after_down = set(inspector.get_table_names())
        assert not (F28_TABLES & after_down), "downgrade left F28 tables"
        assert "workflow_run" in after_down
        cols_after = {c["name"] for c in inspector.get_columns("workflow_run")}
        assert not (F28_RUN_COLUMNS & cols_after), "downgrade left F28 run column"

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
