"""f01 board persistence: DTO round-trip columns + task-dependency adjacency

Backs the DB-backed :class:`forge_board.sql_service.SqlAlchemyBoardService` with
the storage the in-memory board's DTOs need but the base planning tables lacked:

* additive ``epic.spec_id`` + ``epic.labels`` columns (the ``EpicDTO`` fields
  with no dedicated column);
* additive ``task.labels`` column (the ``TaskDTO`` saved-filter label set);
* additive ``sprint.task_ids`` column (the ``SprintDTO`` membership list, distinct
  from the F26 ``committed_task_ids`` velocity snapshot);
* the ``task_dependency`` adjacency table (``TaskDTO.depends_on`` edges), deferred
  from the baseline and created/dropped here from metadata (so the cross-dialect
  column variants apply automatically).

Foundation note (mirrors 0007/0008): ``forge_db``'s baseline is metadata-driven,
so a fresh chain already provisions the new columns from the model. To stay
idiomatic *and* own an explicit, reversible step this migration is **idempotent**:
``upgrade`` adds only what is missing, ``downgrade`` drops only what F01-persist
introduced.

Revision ID: 0024_board_persistence
Revises: 0023_envelope_key_version
Create Date: 2026-07-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)
from forge_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0024_board_persistence"
down_revision: str | None = "0023_envelope_key_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEP_TABLE = "task_dependency"

# JSON column type matching ``forge_db.base.json_type`` (JSONB on Postgres).
_JSON = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")

# (table, column-name, column) additive columns owned by this revision, ordered
# so the downgrade drops them in reverse.
_COLUMNS: tuple[tuple[str, str, sa.Column], ...] = (
    ("epic", "spec_id", sa.Column("spec_id", sa.Uuid(), nullable=True)),
    ("epic", "labels", sa.Column("labels", _JSON, server_default="[]", nullable=False)),
    ("task", "labels", sa.Column("labels", _JSON, server_default="[]", nullable=False)),
    ("sprint", "task_ids", sa.Column("task_ids", _JSON, server_default="[]", nullable=False)),
)


def _dep_table() -> list:
    by_name = {t.name: t for t in Base.metadata.sorted_tables}
    return [by_name[_DEP_TABLE]] if _DEP_TABLE in by_name else []


def _existing_columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    for table, name, column in _COLUMNS:
        if name not in _existing_columns(table):
            op.add_column(table, column)

    Base.metadata.create_all(bind=op.get_bind(), tables=_dep_table())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), tables=_dep_table())

    for table, name, _column in reversed(_COLUMNS):
        if name in _existing_columns(table):
            op.drop_column(table, name)
