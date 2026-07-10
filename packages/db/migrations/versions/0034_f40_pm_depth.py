"""f40 PM depth: sprint calendar columns + capacity/estimation/status-history tables

Extends F26's sprint-velocity foundation with the F40 PM-depth delta:

* additive ``sprint`` columns ``calendar_weekend_days`` / ``calendar_holidays``
  (the working-day/holiday calendar the burndown ideal-line reads —
  ``forge_board.velocity.WorkCalendar``). Both default to an empty JSON list,
  which is byte-identical to the pre-F40 calendar-free ideal line.
* ``sprint_member_capacity`` — a member's declared per-sprint capacity.
* ``estimation_scale`` — a configurable named estimate-value scale.
* ``task_estimate_event`` — append-only estimate-change history (any sprint
  state; Postgres immutability trigger applied via the model's ``after_create``
  listener under ``create_all``, same as ``sprint_scope_event``).
* ``task_status_event`` — append-only status-transition log (portfolio CFD +
  cycle/lead time), same append-only treatment.

None of the four new tables are read by the F26 velocity/burndown rollups —
this migration cannot alter, let alone break, the existing event log.

Idempotent like 0008: ``upgrade`` adds only what is missing; ``downgrade``
drops only what this revision introduced.

Revision ID: 0034_f40_pm_depth
Revises: 0033_f40_scheduled_trigger
Create Date: 2026-07-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)
from forge_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0034_f40_pm_depth"
down_revision: str | None = "0033_f40_scheduled_trigger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SPRINT = "sprint"

PM_DEPTH_TABLES = (
    "sprint_member_capacity",
    "estimation_scale",
    "task_estimate_event",
    "task_status_event",
)

_SPRINT_COLUMNS: tuple[tuple[str, sa.Column], ...] = (
    (
        "calendar_weekend_days",
        sa.Column(
            "calendar_weekend_days",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            server_default="[]",
            nullable=False,
        ),
    ),
    (
        "calendar_holidays",
        sa.Column(
            "calendar_holidays",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            server_default="[]",
            nullable=False,
        ),
    ),
)


def _pm_depth_tables() -> list:
    by_name = {t.name: t for t in Base.metadata.sorted_tables}
    return [by_name[name] for name in PM_DEPTH_TABLES if name in by_name]


def _existing_columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_SPRINT)}


def upgrade() -> None:
    columns = _existing_columns()
    for name, column in _SPRINT_COLUMNS:
        if name not in columns:
            op.add_column(_SPRINT, column)

    Base.metadata.create_all(bind=op.get_bind(), tables=_pm_depth_tables())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), tables=list(reversed(_pm_depth_tables())))

    columns = _existing_columns()
    for name, _column in reversed(_SPRINT_COLUMNS):
        if name in columns:
            op.drop_column(_SPRINT, name)
