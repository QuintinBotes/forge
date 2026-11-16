"""attestation_signing_key: the deployment's stored attestation key (#175)

Adds the one-row ``attestation_signing_key`` table. With no
``FORGE_ATTEST_SIGNING_KEY`` configured, the first process that signs stores an
Ed25519 key there (encrypted under the instance master key), and every later
process signs and verifies with it, so attestations verify across restarts and
between the API and the worker.

Idempotent like 0038-0047: the baseline is metadata-driven, so on a fresh chain
the table already exists and ``upgrade`` changes nothing.

Revision ID: 0048_attestation_signing_key
Revises: 0047_workflow_run_single_active
Create Date: 2026-11-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import scf_db.models  # noqa: F401  (registers all models on Base.metadata)
from scf_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0048_attestation_signing_key"
down_revision: str | None = "0047_workflow_run_single_active"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES: tuple[str, ...] = ("attestation_signing_key",)


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
