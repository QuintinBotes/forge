"""ao-observability: tier + strategy columns on cost_event

Adds two nullable ``cost_event`` columns so Adaptive Orchestration routing
decisions can be aggregated alongside spend:

* ``tier`` — the seniority tier (junior|medior|senior) the ExecutionPlan
  resolved for the role that made this call.
* ``strategy`` — the plan's strategy (single|swarm).

Both are NULL for calls made outside an Adaptive Orchestration plan (e.g. the
cost CLI's ad-hoc commands, or any pre-existing row), so no backfill is
required. A composite index (``workspace_id``, ``tier``, ``occurred_at``)
mirrors the existing ``phase``/``provider`` indexes for the same "cost by
dimension over time" access pattern.

Idempotent like 0026/0027/0028/0029/0030: ``upgrade`` adds only what is
missing, ``downgrade`` drops only what this revision introduced.

Revision ID: 0031_ao_observability_cost_tier
Revises: 0030_ao_settings_api
Create Date: 2026-07-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)

# revision identifiers, used by Alembic.
revision: str = "0031_ao_observability_cost_tier"
down_revision: str | None = "0030_ao_settings_api"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "cost_event"
_COLUMN_NAMES: tuple[str, ...] = ("tier", "strategy")
_INDEX_NAME = "ix_cost_event_tier_time"


def _new_columns() -> list[sa.Column]:
    """Fresh Column objects per call (a Column may be bound to one table only)."""
    return [
        sa.Column("tier", sa.String(length=16), nullable=True),
        sa.Column("strategy", sa.String(length=16), nullable=True),
    ]


def _existing_columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def _existing_indexes() -> set[str]:
    return {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(_TABLE)}


def upgrade() -> None:
    columns = _existing_columns()
    for column in _new_columns():
        if column.name not in columns:
            op.add_column(_TABLE, column)

    if _INDEX_NAME not in _existing_indexes():
        op.create_index(_INDEX_NAME, _TABLE, ["workspace_id", "tier", "occurred_at"])


def downgrade() -> None:
    if _INDEX_NAME in _existing_indexes():
        op.drop_index(_INDEX_NAME, table_name=_TABLE)

    columns = _existing_columns()
    for name in reversed(_COLUMN_NAMES):
        if name in columns:
            op.drop_column(_TABLE, name)
