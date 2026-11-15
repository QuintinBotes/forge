"""workflow_run: one active run per task (#235)

Adds ``uq_workflow_run_active_task``, a partial-unique index on
``workflow_run.task_id`` over rows whose status is not terminal, so the database
refuses a second pending, running or escalated run for a task.

A database that already holds several active runs for one task could not take
the index, so ``upgrade`` first keeps the oldest active run of each such task
and cancels the later ones (``status = 'cancelled'``, ``completed_at`` set).
It cannot reach Temporal: a workflow behind a cancelled row keeps running until
an operator terminates it by the row's ``temporal_workflow_id``.

Idempotent like 0038-0044: ``upgrade`` creates the index only when missing (the
metadata-driven baseline provisions it on a fresh chain); ``downgrade`` drops
only the index and does not revive the runs ``upgrade`` cancelled.

Revision ID: 0045_workflow_run_single_active
Revises: 0044_agent_run_context_refs
Create Date: 2026-11-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0045_workflow_run_single_active"
down_revision: str | None = "0044_agent_run_context_refs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "workflow_run"
_INDEX = "uq_workflow_run_active_task"
_ACTIVE = "status NOT IN ('succeeded', 'failed', 'cancelled')"


def _existing_indexes() -> set[str | None]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(_TABLE)}


def upgrade() -> None:
    if _INDEX in _existing_indexes():
        return
    op.execute(
        """
        UPDATE workflow_run
        SET status = 'cancelled', completed_at = CURRENT_TIMESTAMP
        WHERE status NOT IN ('succeeded', 'failed', 'cancelled')
          AND EXISTS (
            SELECT 1 FROM workflow_run AS older
            WHERE older.task_id = workflow_run.task_id
              AND older.status NOT IN ('succeeded', 'failed', 'cancelled')
              AND (
                older.created_at < workflow_run.created_at
                OR (older.created_at = workflow_run.created_at AND older.id < workflow_run.id)
              )
          )
        """
    )
    op.create_index(
        _INDEX,
        _TABLE,
        ["task_id"],
        unique=True,
        postgresql_where=sa.text(_ACTIVE),
        sqlite_where=sa.text(_ACTIVE),
    )


def downgrade() -> None:
    if _INDEX in _existing_indexes():
        op.drop_index(_INDEX, table_name=_TABLE)
