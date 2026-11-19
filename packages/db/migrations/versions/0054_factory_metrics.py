"""factory_metrics: change outcomes and interventions for the five metrics (#196)

Adds ``change_outcome`` (one row per attempt whose pull request closed: landed or
closed without landing, when, and any later revert) and ``run_intervention`` (a
review that requested changes or a commit by another identity, unique per run,
kind and source so a redelivery writes nothing). Both are workspace-scoped.

Idempotent like 0052: the baseline is metadata-driven, so on a fresh chain the
tables already exist and ``upgrade`` changes nothing.

Revision ID: 0054_factory_metrics
Revises: 0052_device_authorization
Create Date: 2026-11-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import scf_db.models  # noqa: F401  (registers all models on Base.metadata)
from scf_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0054_factory_metrics"
down_revision: str | None = "0052_device_authorization"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES: tuple[str, ...] = ("change_outcome", "run_intervention")


def _owned_tables() -> list[sa.Table]:
    by_name = {t.name: t for t in Base.metadata.sorted_tables}
    return [by_name[name] for name in _TABLES if name in by_name]


def _existing_tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _existing_tables()
    to_create = [t for t in _owned_tables() if t.name not in existing]
    if to_create:
        Base.metadata.create_all(bind=op.get_bind(), tables=to_create)


def downgrade() -> None:
    existing = _existing_tables()
    to_drop = [t for t in reversed(_owned_tables()) if t.name in existing]
    if to_drop:
        Base.metadata.drop_all(bind=op.get_bind(), tables=to_drop)
