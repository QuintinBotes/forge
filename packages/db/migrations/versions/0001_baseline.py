"""baseline: full Forge core data model

Creates the complete spec Core Data Model from ``forge_db`` metadata. Driven
from the SQLAlchemy metadata so the migration can never drift from the models,
and so the cross-dialect column variants (pgvector ``Vector`` -> JSON on SQLite,
``tsvector`` -> TEXT on SQLite) apply automatically.

On Postgres the ``vector`` extension is created first so the embedding column's
``Vector`` type is available.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-06-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)
from forge_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tables owned by later, dedicated migrations (created/dropped there, not here).
# Keeps the baseline focused on the core data model while still metadata-driven.
DEFERRED_TABLES = frozenset(
    {"pm_connection", "pm_task_link", "pm_webhook_delivery"}
)


def _baseline_tables() -> list:
    return [t for t in Base.metadata.sorted_tables if t.name not in DEFERRED_TABLES]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    Base.metadata.create_all(bind=bind, tables=_baseline_tables())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), tables=_baseline_tables())
