"""f26 sprint-velocity: sprint lifecycle columns + 3 derived/log tables

Extends F01's ``sprint`` and adds the F26 sprint-velocity tables:

* additive ``sprint`` columns (started_at, completed_at, capacity_points,
  committed_points, committed_task_count, committed_task_ids, position,
  velocity_version) + the partial unique index ``uq_active_sprint_per_project``
  (``WHERE status = 'active'``) — at most one active sprint per project.
* ``sprint_scope_event`` — append-only log (Postgres immutability trigger applied
  via the model's ``after_create`` listener under ``create_all``).
* ``sprint_burndown_snapshot`` — derived per-day time series.
* ``sprint_velocity`` — derived per-sprint rollup.

Foundation note (mirrors 0007): ``forge_db``'s baseline is metadata-driven, so a
fresh chain already provisions the new ``sprint`` columns/index from the model.
To stay idiomatic *and* own an explicit, reversible step this migration is
**idempotent**: ``upgrade`` adds only what is missing, ``downgrade`` drops only
what F26 introduced. The three new tables are deferred from the baseline and
created/dropped here from metadata (so the cross-dialect column variants and the
append-only trigger apply automatically).

Revision ID: 0008_f26_sprint_velocity
Revises: 0007_f25_temporal_engine
Create Date: 2026-06-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)
from forge_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0008_f26_sprint_velocity"
down_revision: str | None = "0007_f25_temporal_engine"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SPRINT = "sprint"
_ACTIVE_INDEX = "uq_active_sprint_per_project"
_ACTIVE_WHERE = "status = 'active'"

# Ordered so dependents are created after referents and dropped before them.
F26_TABLES = ("sprint_scope_event", "sprint_burndown_snapshot", "sprint_velocity")

_SPRINT_COLUMNS: tuple[tuple[str, sa.Column], ...] = (
    ("started_at", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True)),
    ("completed_at", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True)),
    ("capacity_points", sa.Column("capacity_points", sa.Integer(), nullable=True)),
    (
        "committed_points",
        sa.Column("committed_points", sa.Integer(), server_default="0", nullable=False),
    ),
    (
        "committed_task_count",
        sa.Column("committed_task_count", sa.Integer(), server_default="0", nullable=False),
    ),
    (
        "committed_task_ids",
        sa.Column(
            "committed_task_ids",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            server_default="[]",
            nullable=False,
        ),
    ),
    ("position", sa.Column("position", sa.Text(), nullable=True)),
    (
        "velocity_version",
        sa.Column("velocity_version", sa.BigInteger(), server_default="0", nullable=False),
    ),
)


def _f26_tables() -> list:
    by_name = {t.name: t for t in Base.metadata.sorted_tables}
    return [by_name[name] for name in F26_TABLES if name in by_name]


def _existing_columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_SPRINT)}


def _existing_indexes() -> set[str]:
    return {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(_SPRINT)}


def upgrade() -> None:
    columns = _existing_columns()
    for name, column in _SPRINT_COLUMNS:
        if name not in columns:
            op.add_column(_SPRINT, column)

    if _ACTIVE_INDEX not in _existing_indexes():
        op.create_index(
            _ACTIVE_INDEX,
            _SPRINT,
            ["project_id"],
            unique=True,
            postgresql_where=sa.text(_ACTIVE_WHERE),
            sqlite_where=sa.text(_ACTIVE_WHERE),
        )

    Base.metadata.create_all(bind=op.get_bind(), tables=_f26_tables())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), tables=list(reversed(_f26_tables())))

    if _ACTIVE_INDEX in _existing_indexes():
        op.drop_index(_ACTIVE_INDEX, table_name=_SPRINT)

    columns = _existing_columns()
    for name, _column in reversed(_SPRINT_COLUMNS):
        if name in columns:
            op.drop_column(_SPRINT, name)
