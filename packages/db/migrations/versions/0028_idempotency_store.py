"""idempotency store: HTTP idempotency-key response-cache ``idempotency_key`` table.

Backs the *db* variant of the API's HTTP idempotency store
(:class:`forge_api.middleware.idempotency_db.DbIdempotencyStore` — the storage
boundary behind ``IdempotencyMiddleware``) with real Postgres persistence.
Creates one new, self-contained table:

* ``idempotency_key`` — one row per cached response: the tenant-scoped ``key``
  (UNIQUE — the reserve/get-or-set ``ON CONFLICT`` target), the JSONB
  ``response`` image of the ``StoredResponse`` (``request_hash`` / ``status_code``
  / ``content_type`` / base64 ``body_b64``), the domain record's own tz-aware
  ``created_at``, and the tz-aware ``expires_at`` TTL horizon (indexed for expiry
  sweeps).

Distinct from the per-domain ``automation_execution.idempotency_key`` /
``deployment.idempotency_key`` columns (service-layer command dedup): this is the
transport-level HTTP response cache the middleware owns, so this revision only
*adds* a table and touches nothing existing.

Foundation note (mirrors 0024/0025/0027): ``forge_db``'s metadata is the source
of truth, so a fresh chain already provisions this table from the model. To stay
idiomatic *and* own an explicit, reversible step this migration is idempotent:
``upgrade`` creates only what is missing, ``downgrade`` drops only what this
revision introduced. Applies cleanly on SQLite (unit path) and pgvector Postgres.

Revision ID: 0028_idempotency_store
Revises: 0027_secret_vault_store
Create Date: 2026-07-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)
from forge_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0028_idempotency_store"
down_revision: str | None = "0027_secret_vault_store"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tables owned by this revision (downgrade drops them in reverse).
_TABLES: tuple[str, ...] = ("idempotency_key",)


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
