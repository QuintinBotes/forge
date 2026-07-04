"""container sandboxing: sandbox_instance table (F19)

Creates the F19 ``sandbox_instance`` operational + audit table (deferred from the
baseline, mirroring the PM-adapter pattern). The three additive ``agent_run``
columns (``sandbox_kind`` / ``sandbox_image`` / ``sandbox_container_id``) are part
of the core ``agent_run`` model and are therefore created by the metadata-driven
baseline; this migration owns only the dedicated table.

Revision ID: 0003_container_sandboxing
Revises: 0002_pm_adapters
Create Date: 2026-06-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)
from forge_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0003_container_sandboxing"
down_revision: str | None = "0002_pm_adapters"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SANDBOX_TABLES = ("sandbox_instance",)


def _sandbox_tables() -> list:
    return [t for t in Base.metadata.sorted_tables if t.name in SANDBOX_TABLES]


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), tables=_sandbox_tables())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), tables=_sandbox_tables())
