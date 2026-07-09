"""ss-versioning: spec_version table (Spec Studio version history + diff)

Creates ``spec_version``: an append-per-save, immutable snapshot table backing
Spec Studio's version history + diff view. One row per save of a spec (via
``write_manifest`` / ``save_spec_md`` / ``save_manifest_yaml``), keyed by
``(workspace_id, spec_id, version_number)``, carrying the full manifest
snapshot (JSONB) plus both rendered serializations (``spec.md``,
``manifest.yaml``) so the UI can render a version's content or diff two
versions without recomputing anything from the (mutable, filesystem-backed)
``FileSpecEngine`` state.

Metadata-driven like 0014 (F23 traceability): the table is created wholesale
from the live ``SpecVersion`` model so cross-dialect column variants (JSONB on
Postgres) apply automatically. Purely additive/new table: reversible via a
plain drop.

Revision ID: 0032_ss_versioning
Revises: 0031_ao_observability_cost_tier
Create Date: 2026-07-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)
from forge_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0032_ss_versioning"
down_revision: str | None = "0031_ao_observability_cost_tier"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "spec_version"


def upgrade() -> None:
    table = Base.metadata.tables[_TABLE]
    table.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    table = Base.metadata.tables[_TABLE]
    table.drop(bind=op.get_bind(), checkfirst=True)
