"""f28 workflow visual editor: workflow_definition(_revision) + run pin column

Creates the two editor tables (``workflow_definition`` +
``workflow_definition_revision``) and adds the additive, nullable
``workflow_run.definition_revision_id`` run-pinning column.

The two tables are metadata-driven (``create_all`` over the live models) so the
cross-dialect column variants + the partial single-draft unique index apply
automatically; ``downgrade`` drops them. The ``workflow_run`` column is added/
dropped idempotently (the baseline is metadata-driven, so on a fresh chain the
column already exists — this migration owns a clean, reversible step).

Revision ID: 0010_f28_workflow_editor
Revises: 0009_f27_multi_agent
Create Date: 2026-06-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from forge_db.base import Base
from forge_db.models.workflow_editor import (  # noqa: F401 - registers tables
    WorkflowDefinition,
    WorkflowDefinitionRevision,
)

# revision identifiers, used by Alembic.
revision: str = "0010_f28_workflow_editor"
down_revision: str | None = "0009_f27_multi_agent"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WORKFLOW_RUN = "workflow_run"
_TABLES = ("workflow_definition", "workflow_definition_revision")


def _tables() -> list:
    return [Base.metadata.tables[name] for name in _TABLES]


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    to_create = [t for t in _tables() if t.name not in existing]
    if to_create:
        Base.metadata.create_all(bind=bind, tables=to_create)

    if "definition_revision_id" not in _columns(_WORKFLOW_RUN):
        op.add_column(
            _WORKFLOW_RUN,
            sa.Column("definition_revision_id", sa.Uuid(as_uuid=True), nullable=True),
        )


def downgrade() -> None:
    if "definition_revision_id" in _columns(_WORKFLOW_RUN):
        op.drop_column(_WORKFLOW_RUN, "definition_revision_id")

    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    to_drop = [t for t in reversed(_tables()) if t.name in existing]
    if to_drop:
        Base.metadata.drop_all(bind=bind, tables=to_drop)
