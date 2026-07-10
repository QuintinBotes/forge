"""f40 obs-analytics: skill-profile snapshot, coverage trend, budgets, FX rates

Adds four new, self-contained tables backing the F40-OBS-ANALYTICS deep-analytics
backends (``forge_obs.analytics``):

* ``skill_profile_snapshot`` — immutable per-``agent_run`` capture of the
  resolved skill-profile directives (Postgres immutability trigger applied via
  the model's ``after_create`` listener under ``create_all``, same as
  ``policy_rule_evaluation``).
* ``coverage_snapshot`` — derived per-repo-per-day coverage rollup (mutable,
  idempotent daily upsert; same treatment as ``sprint_burndown_snapshot``).
* ``budget`` — a workspace/project recurring spend cap.
* ``fx_rate`` — an effective-dated currency-conversion price book (global, not
  tenant-scoped; mirrors ``model_price``'s resolution rule).

None of these are read by any existing rollup, so this migration cannot alter,
let alone break, existing behaviour.

Idempotent like 0025/0034: ``upgrade`` creates only what is missing; ``downgrade``
drops only what this revision introduced.

Revision ID: 0035_f40_obs_analytics
Revises: 0034_f40_pm_depth
Create Date: 2026-07-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)
from forge_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0035_f40_obs_analytics"
down_revision: str | None = "0034_f40_pm_depth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES: tuple[str, ...] = (
    "skill_profile_snapshot",
    "coverage_snapshot",
    "budget",
    "fx_rate",
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
