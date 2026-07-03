"""f35 benchmark leaderboard: benchmark_suite + benchmark_submission tables

Creates the two F35 tables from ``forge_db`` metadata (so the migration can
never drift from the models), including the ``uq_benchmark_suite_slug_version``
unique constraint, the status/visibility CHECK constraints, and the leaderboard
covering index ``ix_benchmark_submission_leaderboard``.

``alembic downgrade`` cleanly drops both tables.

Revision ID: 0018_f35_benchmark_leaderboard
Revises: 0017_f34_kernel_sandbox_isolation
Create Date: 2026-07-03
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)
from forge_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0018_f35_benchmark_leaderboard"
down_revision: str | None = "0017_f34_kernel_sandbox_isolation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

F35_TABLES = (
    "benchmark_suite",
    "benchmark_submission",
)


def _f35_tables() -> list:
    by_name = {t.name: t for t in Base.metadata.sorted_tables}
    return [by_name[name] for name in F35_TABLES if name in by_name]


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), tables=_f35_tables())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), tables=list(reversed(_f35_tables())))
