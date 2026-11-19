"""held_out_scenario.workflow_run_id: the run a verifier drafted a scenario for (#288)

Adds the nullable ``workflow_run_id`` (FK to ``workflow_run``, cascading) and its
index, so the spec gate can find the scenarios drafted for its run and withdraw
them when the spec goes back for changes. A scenario a person wrote has none.
The new ``rejected`` and ``withdrawn`` states fit the existing ``state`` column.

Idempotent like 0039: ``upgrade`` adds only what is missing (a fresh chain's
metadata-driven baseline already has both); ``downgrade`` drops only them.

Revision ID: 0051_held_out_scenario_run
Revises: 0050_sign_in_link
Create Date: 2026-11-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0051_held_out_scenario_run"
down_revision: str | None = "0050_sign_in_link"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "held_out_scenario"
_COLUMN = "workflow_run_id"
_INDEX = "ix_held_out_scenario_workflow_run_id"
_FK = "fk_held_out_scenario_workflow_run_id_workflow_run"  # the metadata's naming convention


def _shape() -> tuple[set[str], set[str], set[str]]:
    inspector = sa.inspect(op.get_bind())
    return (
        {c["name"] for c in inspector.get_columns(_TABLE)},
        {ix["name"] for ix in inspector.get_indexes(_TABLE)},
        {fk["name"] for fk in inspector.get_foreign_keys(_TABLE)},
    )


def upgrade() -> None:
    columns, indexes, _ = _shape()
    # Batch mode: SQLite cannot add a foreign key in place; Postgres alters directly.
    with op.batch_alter_table(_TABLE) as batch_op:
        if _COLUMN not in columns:
            batch_op.add_column(sa.Column(_COLUMN, sa.Uuid(as_uuid=True), nullable=True))
            batch_op.create_foreign_key(_FK, "workflow_run", [_COLUMN], ["id"], ondelete="CASCADE")
        if _INDEX not in indexes:
            batch_op.create_index(_INDEX, [_COLUMN])


def downgrade() -> None:
    columns, indexes, fks = _shape()
    with op.batch_alter_table(_TABLE) as batch_op:
        if _INDEX in indexes:
            batch_op.drop_index(_INDEX)
        if _FK in fks:
            batch_op.drop_constraint(_FK, type_="foreignkey")
        if _COLUMN in columns:
            batch_op.drop_column(_COLUMN)
