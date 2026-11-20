"""risk_classification.class_requirements: what each class requires of the gates (#194)

Adds the nullable ``class_requirements`` JSON column: the ``GateRequirements`` of
every risk class, from the repository policy in force when the change was
classified, so the shipping gates read them without the policy. Rows recorded
before it have none, and the gates treat them as unclassified. Adding a column
fires no row trigger, so the table stays append-only.

Idempotent like 0051: ``upgrade`` adds the column only when it is missing (a fresh
chain's metadata-driven 0053 already has it); ``downgrade`` drops it only when present.

Revision ID: 0055_risk_class_requirements
Revises: 0053_risk_classification
Create Date: 2026-11-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from scf_db.base import json_type

# revision identifiers, used by Alembic.
revision: str = "0055_risk_class_requirements"
down_revision: str | None = "0053_risk_classification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "risk_classification"
_COLUMN = "class_requirements"


def _columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def upgrade() -> None:
    if _COLUMN not in _columns():
        op.add_column(_TABLE, sa.Column(_COLUMN, json_type(), nullable=True))


def downgrade() -> None:
    if _COLUMN in _columns():
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.drop_column(_COLUMN)
