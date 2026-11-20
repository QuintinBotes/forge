"""held_automation_action: the exact action a held approval request releases (#191)

Adds ``held_automation_action``: one row per ``automation_action`` approval
request, keeping the irreversible external action and its context unredacted for
the one run an approval allows (the request's payload shows a redacted copy).
``released_at`` marks that run. The ``automation_action`` gate value itself needs
no migration: ``approval_request.gate`` is a VARCHAR sized to the longest value,
which is unchanged.

Idempotent like 0053: the baseline is metadata-driven, so on a fresh chain the
table already exists and ``upgrade`` changes nothing.

Revision ID: 0055_held_automation_action
Revises: 0053_risk_classification
Create Date: 2026-11-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import scf_db.models  # noqa: F401  (registers all models on Base.metadata)
from scf_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0055_held_automation_action"
# 0054 is taken by an open change; rebase this onto it if that lands first.
down_revision: str | None = "0053_risk_classification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "held_automation_action"


def _existing_tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if _TABLE not in _existing_tables():
        Base.metadata.create_all(bind=op.get_bind(), tables=[Base.metadata.tables[_TABLE]])


def downgrade() -> None:
    if _TABLE in _existing_tables():
        Base.metadata.drop_all(bind=op.get_bind(), tables=[Base.metadata.tables[_TABLE]])
