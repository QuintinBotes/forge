"""risk_classification: classified agent changes and gate observations (#193)

Adds ``risk_classification`` (one row per classified agent change) and
``risk_gate_observation`` (one row per gate evaluation beside what risk would have
decided), both workspace-scoped and append-only: on Postgres each table's
immutability trigger is attached when the table is created (``after_create``).

Idempotent like 0050: the baseline is metadata-driven, so on a fresh chain the
tables already exist and ``upgrade`` changes nothing.

Revision ID: 0053_risk_classification
Revises: 0051_held_out_scenario_run
Create Date: 2026-11-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import scf_db.models  # noqa: F401  (registers all models on Base.metadata)
from scf_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0053_risk_classification"
down_revision: str | None = "0051_held_out_scenario_run"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES: tuple[str, ...] = ("risk_classification", "risk_gate_observation")


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
