"""change_outcome.merge_commit_sha: the commit a landed pull request merged (#335)

Adds the nullable ``merge_commit_sha`` column and its index, so the deploy gate
finds a change's classification by the commit that reached the base branch.
Rows recorded before it have none.

Idempotent like 0055: ``upgrade`` adds the column and index only when missing (a
fresh chain's metadata-driven 0054 already has them); ``downgrade`` drops them
only when present.

Revision ID: 0057_change_outcome_merge_commit
Revises: 0056_held_automation_action
Create Date: 2026-11-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0057_change_outcome_merge_commit"
down_revision: str | None = "0056_held_automation_action"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "change_outcome"
_COLUMN = "merge_commit_sha"
_INDEX = "ix_change_outcome_merge_commit"


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def upgrade() -> None:
    if _COLUMN not in {c["name"] for c in _inspector().get_columns(_TABLE)}:
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(64), nullable=True))
    if _INDEX not in {i["name"] for i in _inspector().get_indexes(_TABLE)}:
        op.create_index(_INDEX, _TABLE, [_COLUMN])


def downgrade() -> None:
    if _INDEX in {i["name"] for i in _inspector().get_indexes(_TABLE)}:
        op.drop_index(_INDEX, table_name=_TABLE)
    if _COLUMN in {c["name"] for c in _inspector().get_columns(_TABLE)}:
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.drop_column(_COLUMN)
