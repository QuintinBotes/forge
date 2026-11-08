"""agent run events: the append-only run-event stream (ADR-0013, spec L seam 3)

Adds ``agent_run_event``: one row per event of an agent run, unique on
``(run_id, seq)``, served as Server-Sent Events by ``GET
/agent/runs/{id}/events``. Nothing existing is read or altered.

Idempotent like 0038–0042: ``upgrade`` creates only what is missing;
``downgrade`` drops only what this revision introduced.

Revision ID: 0043_agent_run_event
Revises: 0042_retrieval_bm25_stats
Create Date: 2026-11-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)
from forge_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0043_agent_run_event"
down_revision: str | None = "0042_retrieval_bm25_stats"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES: tuple[str, ...] = ("agent_run_event",)


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
