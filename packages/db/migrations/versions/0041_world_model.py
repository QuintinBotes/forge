"""world model: bitemporal fact-ledger tables

Adds the ten self-contained tables backing the World Model
(``forge_db.models.world``): the minimal ontology (``world_class``,
``world_relation``), identity (``world_entity``, ``world_entity_merge``), the
append-only two-clock ledger (``world_fact`` with its tombstone-only Postgres
trigger and live-dedup partial unique index, ``world_evidence``,
``world_extraction_drop``), governance (``world_proposal``), reasoning proofs
(``world_derivation``) and the decision ledger (``world_decision``). Triggers
are applied via the models' ``after_create`` listeners under ``create_all``,
same as ``attestation`` / ``red_team_record``.

Nothing existing is read or altered, so this migration cannot break existing
behaviour.

Idempotent like 0038: ``upgrade`` creates only what is missing; ``downgrade``
drops only what this revision introduced.

Revision ID: 0041_world_model
Revises: 0040_self_eval_baseline
Create Date: 2026-11-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)
from forge_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0041_world_model"
down_revision: str | None = "0040_self_eval_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES: tuple[str, ...] = (
    "world_class",
    "world_relation",
    "world_entity",
    "world_fact",
    "world_evidence",
    "world_extraction_drop",
    "world_proposal",
    "world_entity_merge",
    "world_derivation",
    "world_decision",
)


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
