"""f31 deployment gates: environment pipeline, environments, deployments + audit

Creates the F31 environment-promotion control-plane tables:

* ``environment_pipeline`` — one ordered pipeline per (project, repo).
* ``environment`` — ordered stages with per-stage gate/provider/health config.
* ``deployment`` — a single promotion attempt (the FSM run); a partial unique
  index (``uq_deployment_active_env``) enforces at-most-one in-flight deployment
  per environment, and ``uq_deployment_idempotency`` dedupes repeat requests.
* ``deployment_transition`` / ``deployment_check_result`` / ``deployment_approval``
  — append-only audit (Postgres immutability trigger via the model's
  ``after_create`` listener; the repository enforces append-only cross-dialect).

Tables are metadata-driven (``create_all`` over the live models) so the
cross-dialect column variants + the append-only triggers apply automatically.
Reversible: ``downgrade`` drops the six tables (children first).

Revision ID: 0013_f31_deployment_gates
Revises: 0012_f30_multi_team_rbac
Create Date: 2026-06-28
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)
from forge_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0013_f31_deployment_gates"
down_revision: str | None = "0012_f30_multi_team_rbac"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Create order matters (parents before children); create_all topologically sorts
# the passed tables, but we keep the list explicit for a readable downgrade.
F31_TABLES = (
    "environment_pipeline",
    "environment",
    "deployment",
    "deployment_transition",
    "deployment_check_result",
    "deployment_approval",
)


def _tables() -> list:
    by_name = {t.name: t for t in Base.metadata.sorted_tables}
    return [by_name[name] for name in F31_TABLES if name in by_name]


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, tables=_tables())


def downgrade() -> None:
    bind = op.get_bind()
    # Drop children first (reverse dependency order).
    for table in reversed(_tables()):
        table.drop(bind=bind, checkfirst=True)
