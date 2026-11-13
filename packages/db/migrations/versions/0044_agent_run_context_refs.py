"""agent-run context refs and world-fact outcome feedback (spec J §2.5)

Adds ``agent_run.context_refs`` (the JSON list of context items a bundle gave
the run) and ``world_fact_feedback`` (append-only outcome rows, unique per
``(run_id, fact_id)``). Nothing existing is read or altered beyond the new
column, which server-defaults to an empty list for pre-existing runs.

Idempotent like 0038-0043: ``upgrade`` creates only what is missing;
``downgrade`` drops only what this revision introduced.

Revision ID: 0044_agent_run_context_refs
Revises: 0043_agent_run_event
Create Date: 2026-11-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)
from forge_db.base import Base, json_type

# revision identifiers, used by Alembic.
revision: str = "0044_agent_run_context_refs"
down_revision: str | None = "0043_agent_run_event"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "world_fact_feedback"
_RUN_TABLE = "agent_run"
_COLUMN = "context_refs"


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _existing_tables() -> set[str]:
    return set(_inspector().get_table_names())


def _run_columns() -> set[str]:
    return {column["name"] for column in _inspector().get_columns(_RUN_TABLE)}


def upgrade() -> None:
    existing = _existing_tables()
    if _TABLE not in existing:
        table = Base.metadata.tables[_TABLE]
        Base.metadata.create_all(bind=op.get_bind(), tables=[table])
    if _RUN_TABLE in existing and _COLUMN not in _run_columns():
        op.add_column(
            _RUN_TABLE,
            sa.Column(_COLUMN, json_type(), server_default=sa.text("'[]'"), nullable=False),
        )


def downgrade() -> None:
    existing = _existing_tables()
    if _RUN_TABLE in existing and _COLUMN in _run_columns():
        op.drop_column(_RUN_TABLE, _COLUMN)
    if _TABLE in existing:
        table = Base.metadata.tables[_TABLE]
        Base.metadata.drop_all(bind=op.get_bind(), tables=[table])
