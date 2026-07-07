"""observability audit store: global hash-chained entry table + chain cursor

Backs the *db* variant of the API's observability audit store
(``forge_api.observability.audit`` — the platform sink the MCP db-path forwards
to) with real Postgres persistence. Creates two new, self-contained tables:

* ``observability_audit_entry`` — one append-only row per audit entry, carrying
  the global tamper-evident hash chain (``seq`` / ``prev_hash`` / ``entry_hash``)
  plus the entry payload (``category`` / ``actor`` / ``run_id`` /
  ``connection_id`` / optional ``workspace_id`` / ``metadata`` / ...);
* ``observability_audit_chain_head`` — the single global cursor row that
  serializes appends and hands out the next ``seq`` / ``prev_hash``.

These are distinct from F39's per-workspace ``audit_log`` / ``audit_chain_head``
(different sink, different chain semantics — see the model module docstring), so
this revision only *adds* tables and touches nothing existing.

Foundation note (mirrors 0024): ``forge_db``'s metadata is the source of truth,
so a fresh chain already provisions these tables from the models. To stay
idiomatic *and* own an explicit, reversible step this migration is idempotent:
``upgrade`` creates only what is missing, ``downgrade`` drops only what this
revision introduced. Applies cleanly on SQLite (unit path) and pgvector Postgres.

Revision ID: 0025_observability_audit_store
Revises: 0024_board_persistence
Create Date: 2026-07-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)
from forge_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0025_observability_audit_store"
down_revision: str | None = "0024_board_persistence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tables owned by this revision, in create order (no inter-table FK, so order is
# cosmetic; downgrade drops them in reverse).
_TABLES: tuple[str, ...] = (
    "observability_audit_entry",
    "observability_audit_chain_head",
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
