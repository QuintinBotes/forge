"""f32 integration marketplace: registry/listing/version/installation/audit tables

Creates the five workspace-scoped marketplace tables from ``forge_db`` metadata
(so the migration can never drift from the models), including:

* the unique + check constraints (``uq_marketplace_registry_slug``, the
  ``type``/``trust_level`` CHECKs, ``uq_marketplace_listing_registry_kind_slug``,
  ``uq_marketplace_installation_pkg`` …),
* the btree indexes,
* the Postgres-only GIN(tags) + full-text ``to_tsvector('english', name || ' ' ||
  summary)`` catalog-search indexes (installed via the models' ``after_create``
  listeners, guarded to the postgresql dialect — a clean no-op on SQLite), and
* the ``marketplace_audit_log_immutable`` BEFORE UPDATE/DELETE trigger (the F39
  ``attach_immutability_trigger`` helper, applied the same way).

``alembic downgrade`` cleanly drops all five tables (and their trigger/indexes).

Revision ID: 0015_f32_integration_marketplace
Revises: 0014_f23_spec_validation_dashboard
Create Date: 2026-07-01
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)
from forge_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0015_f32_integration_marketplace"
down_revision: str | None = "0014_f23_spec_validation_dashboard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

F32_TABLES = (
    "marketplace_registry",
    "marketplace_listing",
    "marketplace_listing_version",
    "marketplace_installation",
    "marketplace_audit_log",
)


def _f32_tables() -> list:
    by_name = {t.name: t for t in Base.metadata.sorted_tables}
    return [by_name[name] for name in F32_TABLES if name in by_name]


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), tables=_f32_tables())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), tables=list(reversed(_f32_tables())))
