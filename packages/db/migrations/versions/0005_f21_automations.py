"""f21 automations: automation_rule + automation_execution tables

Creates the two F21 tables (deferred from the baseline, mirroring the
PM-adapter / sandbox / MCP-index migration pattern):

* ``automation_rule`` — saved WHEN/IF/THEN rules with the partial dispatch index
  ``ix_automation_rule_dispatch (workspace_id, project_id, trigger_type) WHERE enabled``.
* ``automation_execution`` — append-only audit rows, deduped by the
  ``(rule_id, trigger_event_id)`` idempotency key and hardened on Postgres with
  the F39 ``attach_immutability_trigger`` BEFORE UPDATE/DELETE block (applied via
  the model's ``after_create`` event listener, so this ``create_all`` path
  installs it automatically).

The FKs (project, app_user) resolve against the baseline tables.

Revision ID: 0005_f21_automations
Revises: 0004_mcp_sync_and_index
Create Date: 2026-06-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)
from forge_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0005_f21_automations"
down_revision: str | None = "0004_mcp_sync_and_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Ordered so the dependent table (automation_execution -> automation_rule) is
# created after its referent and dropped before it.
F21_TABLES = ("automation_rule", "automation_execution")


def _f21_tables() -> list:
    by_name = {t.name: t for t in Base.metadata.sorted_tables}
    return [by_name[name] for name in F21_TABLES if name in by_name]


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), tables=_f21_tables())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), tables=list(reversed(_f21_tables())))
