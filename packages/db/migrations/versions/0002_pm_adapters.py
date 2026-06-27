"""F18: external PM-adapter tables (Jira, Linear)

Creates ``pm_connection`` / ``pm_task_link`` / ``pm_webhook_delivery`` from the
``forge_db`` metadata (same no-drift approach as the baseline). These three
tables are deferred out of ``0001_baseline`` (see its ``DEFERRED_TABLES``) so
this migration owns their create/drop lifecycle.

Revision ID: 0002_pm_adapters
Revises: 0001_baseline
Create Date: 2026-06-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)
from forge_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0002_pm_adapters"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PM_TABLES = ("pm_connection", "pm_task_link", "pm_webhook_delivery")


def _pm_tables() -> list:
    tables = Base.metadata.tables
    # sorted_tables order respects FK dependencies (pm_connection before links).
    return [t for t in Base.metadata.sorted_tables if t.name in PM_TABLES]


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), tables=_pm_tables())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), tables=_pm_tables())
