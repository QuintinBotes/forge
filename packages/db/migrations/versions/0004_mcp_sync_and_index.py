"""mcp sync-and-index: knowledge_sync_run + mcp_indexed_resource tables (F20)

Creates the two F20 tables (deferred from the baseline, mirroring the PM-adapter
and sandbox migration pattern):

* ``knowledge_sync_run`` — one row per sync run (counters, status, timing).
* ``mcp_indexed_resource`` — per-resource sync ledger (change-token / content-hash
  change detection, tombstoning, provenance), with the ``ux_mcp_idx_resource``
  unique index, the tenant/seen indexes, and the
  ``deleted_at IS NULL OR chunk_count = 0`` CHECK constraint.

No new Postgres extension is needed (``vector`` is enabled by the baseline). The
ancestry includes F05's knowledge tables and F09's ``mcp_connection`` (both in the
baseline) so the FKs resolve.

Revision ID: 0004_mcp_sync_and_index
Revises: 0003_container_sandboxing
Create Date: 2026-06-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)
from forge_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0004_mcp_sync_and_index"
down_revision: str | None = "0003_container_sandboxing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Ordered so the dependent table (mcp_indexed_resource -> knowledge_sync_run)
# is created after its referent and dropped before it.
F20_TABLES = ("knowledge_sync_run", "mcp_indexed_resource")


def _f20_tables() -> list:
    by_name = {t.name: t for t in Base.metadata.sorted_tables}
    return [by_name[name] for name in F20_TABLES if name in by_name]


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), tables=_f20_tables())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), tables=list(reversed(_f20_tables())))
