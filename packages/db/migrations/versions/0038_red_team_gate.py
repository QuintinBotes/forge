"""red-team gate: append-only red_team_record verdict table

Adds one new, self-contained table backing the "Red-Team Gate" feature
(``forge_db.models.red_team``):

* ``red_team_record`` — an immutable per-scan adversarial-review verdict
  (``blocked``/``survived``, the attack ``kind``, the structured ``evidence``,
  and the heterogeneous ``adversary_model`` vs ``coder_model`` pair), hardened
  with the shared F39 Postgres immutability trigger (applied via the model's
  ``after_create`` listener under ``create_all``, same as ``attestation`` /
  ``run_recording``).

Nothing existing is read or altered, so this migration cannot break existing
behaviour.

Idempotent like 0036/0037: ``upgrade`` creates only what is missing;
``downgrade`` drops only what this revision introduced.

Revision ID: 0038_red_team_gate
Revises: 0037_time_travel_runs
Create Date: 2026-07-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)
from forge_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0038_red_team_gate"
down_revision: str | None = "0037_time_travel_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES: tuple[str, ...] = ("red_team_record",)


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
