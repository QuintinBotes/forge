"""f37 auth-secrets-byok: platform_api_key + oauth_account

Creates the two F37 identity tables (spec §3.1):

* ``platform_api_key`` — inbound machine/agent auth tokens (one-way peppered
  HMAC hash + embedded public ``key_id``; unique ``key_id``, the
  ``agent_runner → expires_at`` CHECK, and the partial expiring index).
* ``oauth_account`` — linked external OAuth identities → one Forge user
  (globally unique ``(provider, provider_subject)``).

The BYOK table (``api_key``) and identity tables (``workspace`` / ``app_user``)
already exist from the baseline and are untouched; the central ``audit_log``
is owned by ``cross-cutting/F39-audit-log`` (created in 0012) and is NOT
touched here — F37 only emits events into it.

Tables are metadata-driven (``create_all`` over the live models) so the
cross-dialect column variants apply automatically, and the upgrade is
idempotent on a fresh metadata-driven chain (mirrors 0012/0019). Downgrade
drops only the two F37 tables.

Revision ID: 0020_f37_auth_secrets
Revises: 0019_f36_approval_framework
Create Date: 2026-07-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)
from forge_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0020_f37_auth_secrets"
down_revision: str | None = "0019_f36_approval_framework"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

F37_TABLES = ("platform_api_key", "oauth_account")


def _f37_tables() -> list:
    by_name = {t.name: t for t in Base.metadata.sorted_tables}
    return [by_name[name] for name in F37_TABLES if name in by_name]


def _existing_tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _existing_tables()
    tables = [t for t in _f37_tables() if t.name not in existing]
    if tables:
        Base.metadata.create_all(bind=op.get_bind(), tables=tables)


def downgrade() -> None:
    existing = _existing_tables()
    tables = [t for t in _f37_tables() if t.name in existing]
    if tables:
        Base.metadata.drop_all(bind=op.get_bind(), tables=tables)
