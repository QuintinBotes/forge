"""ao-settings-api: workspace-wide Adaptive Orchestration settings table.

Creates one new, self-contained table:

* ``ao_workspace_settings`` -- one row per workspace holding the ``auto_route``
  toggle, the ``tier -> model`` overrides layered onto the model router's
  defaults (``tier_model_overrides``, JSONB ``{provider: {tier: model}}``), and
  the complexity-score thresholds (``junior_max``/``medior_max``, ``NULL`` =
  use the hardcoded default). A unique index on ``workspace_id`` alone enforces
  the one-row-per-workspace invariant.

Idempotent like 0024/0025/0027/0028/0029: ``upgrade`` creates only what is
missing, ``downgrade`` drops only what this revision introduced.

Revision ID: 0030_ao_settings_api
Revises: 0029_ao_config_role_model_config
Create Date: 2026-07-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)
from forge_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0030_ao_settings_api"
down_revision: str | None = "0029_ao_config_role_model_config"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tables owned by this revision (downgrade drops them in reverse).
_TABLES: tuple[str, ...] = ("ao_workspace_settings",)


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
