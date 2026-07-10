"""f40 automations: SCHEDULED trigger type + SCHEDULER source vocabulary

F40 adds two members to the F21 automation enums:

* ``AutomationTriggerType.SCHEDULED`` (``"scheduled"``) — a Celery-Beat cron
  trigger whose cadence lives in ``automation_rule.trigger_config['cron']``.
* ``AutomationTriggerSource.SCHEDULER`` (``"scheduler"``) — the Beat producer.

The automation enum columns are stored as ``VARCHAR`` (``enum_type`` renders
``native_enum=False`` with no native ENUM/CHECK), so the only schema concern is
that the columns are wide enough for the new string values. The baseline widths
were sized to the longest F21 value (``workflow_state_changed`` = 22); the new
values fit, but this revision widens the ``trigger_type`` / ``trigger_source``
columns to ``VARCHAR(32)`` on Postgres for headroom and as the documented schema
revision point for the F40 vocabulary. No CHECK constraint exists to amend and
no data backfill is required.

Idempotent like 0026-0032: ``upgrade`` widens only when the current width is
narrower than the target; ``downgrade`` is a no-op (narrowing a VARCHAR that may
hold existing values is unsafe, and the wider column is a strict superset).

Revision ID: 0033_f40_scheduled_trigger
Revises: 0032_ss_versioning
Create Date: 2026-07-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)

# revision identifiers, used by Alembic.
revision: str = "0033_f40_scheduled_trigger"
down_revision: str | None = "0032_ss_versioning"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TARGET_LEN = 32
# (table, column) pairs whose enum-backed VARCHAR should hold the F40 values.
_ENUM_COLUMNS: tuple[tuple[str, str], ...] = (
    ("automation_rule", "trigger_type"),
    ("automation_execution", "trigger_type"),
    ("automation_execution", "trigger_source"),
)


def _column_length(table: str, column: str) -> int | None:
    """Current declared VARCHAR length of ``table.column`` (``None`` if unknown)."""
    for col in sa.inspect(op.get_bind()).get_columns(table):
        if col["name"] == column:
            return getattr(col["type"], "length", None)
    return None


def upgrade() -> None:
    # SQLite (unit tests) provisions via create_all against the current models, so
    # the columns are already correct there; only Postgres needs the ALTER.
    if op.get_bind().dialect.name != "postgresql":
        return
    for table, column in _ENUM_COLUMNS:
        current = _column_length(table, column)
        if current is None or current < _TARGET_LEN:
            op.alter_column(
                table,
                column,
                type_=sa.String(length=_TARGET_LEN),
                existing_nullable=False,
            )


def downgrade() -> None:
    # No-op: narrowing a VARCHAR that may already hold 'scheduled'/'scheduler'
    # values is unsafe, and a wider column is a strict superset of the old one.
    return
