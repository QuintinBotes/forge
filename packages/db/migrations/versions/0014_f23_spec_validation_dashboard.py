"""f23 spec-validation-dashboard: traceability projection tables

Creates the two denormalised read-model tables that power the project-scoped
Spec Validation Dashboard (F23):

* ``traceability_criterion_link`` — one row per acceptance criterion per spec
  (UNIQUE(spec_id, criterion_ext_id); indexed on (project_id, status), project_id
  and spec_id for the matrix + filtered scans).
* ``traceability_spec_rollup`` — one row per spec (UNIQUE(spec_id); indexed on
  (project_id, validation_status), project_id and epic_id) carrying the monotonic
  ``projection_version`` the UI's "Recompute" polls.

Both are derived state, rebuildable from F02 (+ F08) source rows by the
``TraceabilityProjector``; nothing reads them as source of truth and no gate
consults them. Tables are metadata-driven (``create_all`` over the live models)
so the cross-dialect column variants (JSONB on Postgres) apply automatically.
Reversible: ``downgrade`` drops both tables (children first).

Foundation note: the slice doc's additive ``spec_validation_reports.spec_version``
column and ``traceability_criterion_link.last_report_id`` FK are omitted — this
foundation has no ``spec_validation_reports`` table (F02 validation is
filesystem-backed); staleness is driven by the plain ``report_spec_version`` /
``current_spec_version`` int columns instead.

Revision ID: 0014_f23_spec_dashboard
Revises: 0013_f31_deployment_gates
Create Date: 2026-06-29
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)
from forge_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0014_f23_spec_dashboard"
down_revision: str | None = "0013_f31_deployment_gates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

F23_TABLES = (
    "traceability_criterion_link",
    "traceability_spec_rollup",
)


def _tables() -> list:
    by_name = {t.name: t for t in Base.metadata.sorted_tables}
    return [by_name[name] for name in F23_TABLES if name in by_name]


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), tables=_tables())


def downgrade() -> None:
    bind = op.get_bind()
    for table in reversed(_tables()):
        table.drop(bind=bind, checkfirst=True)
