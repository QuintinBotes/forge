"""self-eval gate: benchmark_suite workspace/repo scoping + private flag

Extends ``benchmark_suite`` (``forge_db.models.benchmark.BenchmarkSuite``)
with three *nullable*/defaulted columns so an org can mint a PRIVATE,
per-repo regression suite from its own merged PRs (F41 "Self-Eval Gate"),
without disturbing any existing global/public suite row:

* ``workspace_id`` — nullable FK to ``workspace``; NULL preserves today's
  "shared/community suite" semantics for every pre-existing row.
* ``repo_id`` — nullable free-form source-repository identifier (no FK,
  mirrors ``repository_connection.repo_id``).
* ``private`` — ``NOT NULL`` with server default ``false``, so existing rows
  read as public/community (unchanged) and only newly-minted self-eval
  suites opt into ``private=true``.

Also adds the ``ix_benchmark_suite_workspace_id`` lookup index.

Nothing existing is dropped or renamed, so this migration cannot break
existing behaviour.

Idempotent like 0026/0036/0037/0038: ``upgrade`` adds only what is missing;
``downgrade`` drops only what this revision introduced.

Revision ID: 0039_self_eval_suite_scoping
Revises: 0038_red_team_gate
Create Date: 2026-07-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)

# revision identifiers, used by Alembic.
revision: str = "0039_self_eval_suite_scoping"
down_revision: str | None = "0038_red_team_gate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "benchmark_suite"
# Column names owned by this revision, in add order (downgrade drops in reverse).
_COLUMN_NAMES: tuple[str, ...] = ("workspace_id", "repo_id", "private")
_INDEX_NAME = "ix_benchmark_suite_workspace_id"


def _new_columns() -> list[sa.Column]:
    """Fresh Column objects per call (a Column may be bound to one table only)."""
    return [
        sa.Column(
            "workspace_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("workspace.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("repo_id", sa.String(length=512), nullable=True),
        sa.Column(
            "private",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    ]


def _existing_columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def _existing_indexes() -> set[str]:
    return {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes(_TABLE)}


def upgrade() -> None:
    columns = _existing_columns()
    for column in _new_columns():
        if column.name not in columns:
            op.add_column(_TABLE, column)

    if _INDEX_NAME not in _existing_indexes():
        op.create_index(_INDEX_NAME, _TABLE, ["workspace_id"])


def downgrade() -> None:
    # SQLite has no native DROP COLUMN/CONSTRAINT for an FK'd column; batch
    # mode recreates the table under the hood. Postgres uses the same batch
    # API but takes the direct ALTER TABLE path (no recreate needed).
    columns = _existing_columns()
    indexes = _existing_indexes()
    to_drop = [name for name in reversed(_COLUMN_NAMES) if name in columns]
    if not to_drop and _INDEX_NAME not in indexes:
        return

    with op.batch_alter_table(_TABLE) as batch_op:
        if _INDEX_NAME in indexes:
            batch_op.drop_index(_INDEX_NAME)
        for name in to_drop:
            batch_op.drop_column(name)
