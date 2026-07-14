"""baseline: full Forge core data model

Creates the complete spec Core Data Model from ``forge_db`` metadata. Driven
from the SQLAlchemy metadata so the migration can never drift from the models,
and so the cross-dialect column variants (pgvector ``Vector`` -> JSON on SQLite,
``tsvector`` -> TEXT on SQLite) apply automatically.

On Postgres the ``vector`` extension is created first so the embedding column's
``Vector`` type is available.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-06-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)
from forge_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tables owned by later, dedicated migrations (created/dropped there, not here).
# Keeps the baseline focused on the core data model while still metadata-driven.
DEFERRED_TABLES = frozenset(
    {
        "pm_connection",
        "pm_task_link",
        "pm_webhook_delivery",
        # F19 container-sandboxing — created by 0003_container_sandboxing.
        "sandbox_instance",
        # F20 MCP sync-and-index — created by 0004_mcp_sync_and_index.
        "knowledge_sync_run",
        "mcp_indexed_resource",
        # F21 automations — created by 0005_f21_automations.
        "automation_rule",
        "automation_execution",
        # F22 multi-repo execution — created by 0006_f22_multi_repo.
        "pr_group",
        "agent_repo_workspace",
        # F26 sprint-velocity — created by 0008_f26_sprint_velocity.
        "sprint_scope_event",
        "sprint_burndown_snapshot",
        "sprint_velocity",
        # F28 workflow visual editor — created by 0010_f28_workflow_editor.
        "workflow_definition",
        "workflow_definition_revision",
        # F29 advanced-policy-engine — created by 0011_f29_policy_rule_evaluation.
        "policy_rule_evaluation",
        # F30 multi-team RBAC — created by 0012_f30_multi_team_rbac.
        "team",
        "team_member",
        "project_team_access",
        "role_grant",
        "audit_log",
        # F23 spec-validation-dashboard — created by
        # 0014_f23_spec_dashboard.
        "traceability_criterion_link",
        "traceability_spec_rollup",
        # F32 integration-marketplace — created by
        # 0015_f32_integration_marketplace.
        "marketplace_registry",
        "marketplace_listing",
        "marketplace_listing_version",
        "marketplace_installation",
        "marketplace_audit_log",
        # F35 benchmark-leaderboard — created by 0018_f35_benchmark_leaderboard.
        "benchmark_suite",
        "benchmark_submission",
        # F41 Self-Eval Gate baseline (FK -> benchmark_suite, itself deferred) —
        # created by 0040_self_eval_baseline.
        "self_eval_baseline",
        # F01 board persistence (task dependency adjacency) — created by
        # 0024_board_persistence.
        "task_dependency",
    }
)


def _baseline_tables() -> list:
    return [t for t in Base.metadata.sorted_tables if t.name not in DEFERRED_TABLES]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    Base.metadata.create_all(bind=bind, tables=_baseline_tables())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), tables=_baseline_tables())
