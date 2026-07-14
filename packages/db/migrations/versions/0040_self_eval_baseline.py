"""self-eval gate: per-(workspace, suite) baseline resolution rate

Adds the ``self_eval_baseline`` table (``forge_db.models.benchmark.
SelfEvalBaseline``): the frozen resolution rate a later model/prompt/router
change is gated against. Exactly one baseline per (workspace, suite) — a new
run upserts the row — so the Self-Eval Gate can look up "the rate this config
must not fall below" for a workspace's private per-repo suite.

Purely additive: a brand-new table, no change to any existing table, so it
cannot alter current behaviour. Idempotent like 0036-0039: ``upgrade`` creates
the table only if absent; ``downgrade`` drops only what this revision adds.

Revision ID: 0040_self_eval_baseline
Revises: 0039_self_eval_suite_scoping
Create Date: 2026-07-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)

# revision identifiers, used by Alembic.
revision: str = "0040_self_eval_baseline"
down_revision: str | None = "0039_self_eval_suite_scoping"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "self_eval_baseline"
_INDEX_NAME = "ix_self_eval_baseline_workspace"
_UNIQUE_NAME = "uq_self_eval_baseline_workspace_suite"


def _existing_tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if _TABLE in _existing_tables():
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("workspace.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "benchmark_suite_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("benchmark_suite.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("baseline_rate", sa.Float(), nullable=False),
        sa.Column("resolved", sa.Integer(), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column(
            "config",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "recorded_by",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("app_user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.UniqueConstraint("workspace_id", "benchmark_suite_id", name=_UNIQUE_NAME),
    )
    op.create_index(_INDEX_NAME, _TABLE, ["workspace_id"])


def downgrade() -> None:
    if _TABLE not in _existing_tables():
        return
    indexes = {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes(_TABLE)}
    if _INDEX_NAME in indexes:
        op.drop_index(_INDEX_NAME, table_name=_TABLE)
    op.drop_table(_TABLE)
