"""secret vault store: envelope-encrypted BYOK ``secret`` table.

Backs the *db* variant of the API's secret vault store
(``forge_api.auth.vault`` — the storage boundary behind ``SecretVault``) with
real Postgres persistence. Creates one new, self-contained table:

* ``secret`` — one row per stored :class:`~forge_api.auth.vault.StoredSecret`:
  the envelope-encrypted ``ciphertext`` (no plaintext, ever), its
  :class:`~forge_contracts.enums.APIKeyKind`, per-workspace scope
  (``workspace_id`` FK, CASCADE), the HARD-13 envelope ``key_version`` /
  ``rotated_at`` (KEK-rotation bookkeeping, mirroring 0023's ``api_key`` columns),
  ``expires_at`` (read-time expiry), ``last_used_at``, display-safe ``key_prefix``,
  ``created_at`` / ``updated_at`` (the domain record's own timestamps), and a
  reserved ``secret_metadata`` JSONB bag.

Distinct from ``api_key`` (F37's BYOK row, a different code path) and
``platform_api_key`` (inbound verify-only auth), so this revision only *adds* a
table and touches nothing existing.

Foundation note (mirrors 0024/0025): ``forge_db``'s metadata is the source of
truth, so a fresh chain already provisions this table from the model. To stay
idiomatic *and* own an explicit, reversible step this migration is idempotent:
``upgrade`` creates only what is missing, ``downgrade`` drops only what this
revision introduced. Applies cleanly on SQLite (unit path) and pgvector Postgres.

Revision ID: 0027_secret_vault_store
Revises: 0026_approval_repository_columns
Create Date: 2026-07-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)
from forge_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0027_secret_vault_store"
down_revision: str | None = "0026_approval_repository_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tables owned by this revision (downgrade drops them in reverse).
_TABLES: tuple[str, ...] = ("secret",)


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
