"""f22 multi-repo execution: pr_group + agent_repo_workspace tables

Creates the two F22 tables (deferred from the baseline, mirroring the
PM-adapter / sandbox / MCP-index / automation migration pattern):

* ``pr_group`` — one row per multi-repo run's PR set (the merge unit), unique per
  ``workflow_run_id``, carrying the topological ``merge_order`` and the
  ``merged_repo_ids`` partial-merge audit trail.
* ``agent_repo_workspace`` — one worktree row per ``(agent_run, repo)``, unique on
  ``(agent_run_id, repo_id)``.

The FKs (workflow_run, task, agent_run) resolve against the baseline tables.

Revision ID: 0006_f22_multi_repo
Revises: 0005_f21_automations
Create Date: 2026-06-28
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)
from forge_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0006_f22_multi_repo"
down_revision: str | None = "0005_f21_automations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Ordered so independent tables create cleanly and drop in reverse.
F22_TABLES = ("pr_group", "agent_repo_workspace")


def _f22_tables() -> list:
    by_name = {t.name: t for t in Base.metadata.sorted_tables}
    return [by_name[name] for name in F22_TABLES if name in by_name]


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), tables=_f22_tables())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), tables=list(reversed(_f22_tables())))
