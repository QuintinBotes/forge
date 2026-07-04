"""f29 advanced policy engine: policy_rule_evaluation append-only audit table

Creates the single F29 table — the immutable per-decision record written when a
conditional policy rule contributes to an evaluation. It carries its two indexes
(``ix_policy_rule_evaluation_agent_run_id`` and
``ix_policy_rule_evaluation_workspace_evaluated``) and is hardened on Postgres
with the F39 ``attach_immutability_trigger`` BEFORE UPDATE/DELETE block (applied
via the model's ``after_create`` event listener, so this ``create_all`` path
installs it automatically).

Additive and backward-compatible; the ``agent_run_id`` FK resolves against the
F07/F10 ``agent_run`` baseline table.

Revision ID: 0011_f29_policy_rule_evaluation
Revises: 0010_f28_workflow_editor
Create Date: 2026-06-28
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)
from forge_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0011_f29_policy_rule_evaluation"
down_revision: str | None = "0010_f28_workflow_editor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

F29_TABLES = ("policy_rule_evaluation",)


def _f29_tables() -> list:
    by_name = {t.name: t for t in Base.metadata.sorted_tables}
    return [by_name[name] for name in F29_TABLES if name in by_name]


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), tables=_f29_tables())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), tables=list(reversed(_f29_tables())))
