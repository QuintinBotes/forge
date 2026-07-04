"""f27 supervised multi-agent: agent_run coordinator columns + sub_agent_run fields

Adds the F27 Supervisor columns:

* ``agent_run.is_supervisor`` / ``pattern`` / ``supervision`` — mark the parent
  coordinator run, record the chosen ``CoordinationPattern``, and store the
  resolved plan + merge summary (redacted).
* ``sub_agent_run`` gains the per-subagent fields (``agent_run_id``,
  ``assignment_id``, ``pattern``, ``ordinal``, ``depends_on``, ``optional``,
  ``objective``, ``artifact``, ``branch_name``, ``merged``, ``token_usage``,
  ``error``) plus the parent/child indexes and the
  ``(parent_agent_run_id, assignment_id)`` unique constraint.

Foundation note: ``forge_db``'s baseline (``0001_baseline``) is metadata-driven
(``create_all`` over the live models), so once these columns live on the models
the baseline already provisions them on a fresh chain. To stay idiomatic *and*
own an explicit, reversible F27 step this migration is **idempotent**: ``upgrade``
adds only what is missing, ``downgrade`` drops only what F27 introduced (never the
``agent_run`` / ``sub_agent_run`` tables, which the baseline owns).

Revision ID: 0009_f27_multi_agent
Revises: 0008_f26_sprint_velocity
Create Date: 2026-06-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009_f27_multi_agent"
down_revision: str | None = "0008_f26_sprint_velocity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AGENT_RUN = "agent_run"
_SUB_AGENT_RUN = "sub_agent_run"

_UQ_ASSIGNMENT = "uq_sub_agent_run_assignment"
_IX_PARENT = "ix_sub_agent_run_parent"
_IX_CHILD = "ix_sub_agent_run_child"


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    agent_cols = _columns(_AGENT_RUN)
    if "is_supervisor" not in agent_cols:
        op.add_column(
            _AGENT_RUN,
            sa.Column(
                "is_supervisor",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
            ),
        )
    if "pattern" not in agent_cols:
        op.add_column(_AGENT_RUN, sa.Column("pattern", sa.String(length=64), nullable=True))
    if "supervision" not in agent_cols:
        op.add_column(
            _AGENT_RUN,
            sa.Column(
                "supervision",
                sa.JSON(),
                server_default=sa.text("'{}'"),
                nullable=False,
            ),
        )

    sub_cols = _columns(_SUB_AGENT_RUN)
    _add = [
        ("agent_run_id", sa.Column("agent_run_id", sa.Uuid(as_uuid=True), nullable=True)),
        (
            "assignment_id",
            sa.Column(
                "assignment_id",
                sa.String(length=128),
                server_default=sa.text("''"),
                nullable=False,
            ),
        ),
        ("pattern", sa.Column("pattern", sa.String(length=64), nullable=True)),
        (
            "ordinal",
            sa.Column("ordinal", sa.Integer(), server_default=sa.text("0"), nullable=False),
        ),
        (
            "depends_on",
            sa.Column("depends_on", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        ),
        (
            "optional",
            sa.Column(
                "optional", sa.Boolean(), server_default=sa.text("false"), nullable=False
            ),
        ),
        (
            "objective",
            sa.Column("objective", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        ),
        (
            "artifact",
            sa.Column("artifact", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        ),
        ("branch_name", sa.Column("branch_name", sa.String(length=255), nullable=True)),
        (
            "merged",
            sa.Column("merged", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        ),
        (
            "token_usage",
            sa.Column(
                "token_usage", sa.JSON(), server_default=sa.text("'{}'"), nullable=False
            ),
        ),
        ("error", sa.Column("error", sa.JSON(), nullable=True)),
    ]
    for name, column in _add:
        if name not in sub_cols:
            op.add_column(_SUB_AGENT_RUN, column)

    indexes = _indexes(_SUB_AGENT_RUN)
    if _IX_PARENT not in indexes:
        op.create_index(_IX_PARENT, _SUB_AGENT_RUN, ["parent_agent_run_id", "ordinal"])
    if _IX_CHILD not in indexes:
        op.create_index(_IX_CHILD, _SUB_AGENT_RUN, ["agent_run_id"])
    # Assignment uniqueness is a unique INDEX (cross-dialect droppable).
    if _UQ_ASSIGNMENT not in _indexes(_SUB_AGENT_RUN):
        op.create_index(
            _UQ_ASSIGNMENT, _SUB_AGENT_RUN, ["parent_agent_run_id", "assignment_id"], unique=True
        )

    # The child-run FK is Postgres-only (SQLite keeps a plain column so the
    # downgrade can drop it). Add it when missing on a fresh Postgres chain.
    if op.get_bind().dialect.name == "postgresql":
        fk_names = {fk["name"] for fk in sa.inspect(op.get_bind()).get_foreign_keys(_SUB_AGENT_RUN)}
        if "fk_sub_agent_run_child" not in fk_names:
            op.create_foreign_key(
                "fk_sub_agent_run_child",
                _SUB_AGENT_RUN,
                _AGENT_RUN,
                ["agent_run_id"],
                ["id"],
                ondelete="SET NULL",
            )


def downgrade() -> None:
    # Drop the FK first (Postgres) so the column becomes droppable, then drop the
    # indexes (so their columns become droppable on SQLite), then the columns.
    if op.get_bind().dialect.name == "postgresql":
        fks = {fk["name"] for fk in sa.inspect(op.get_bind()).get_foreign_keys(_SUB_AGENT_RUN)}
        if "fk_sub_agent_run_child" in fks:
            op.drop_constraint("fk_sub_agent_run_child", _SUB_AGENT_RUN, type_="foreignkey")

    indexes = _indexes(_SUB_AGENT_RUN)
    for index in (_UQ_ASSIGNMENT, _IX_CHILD, _IX_PARENT):
        if index in indexes:
            op.drop_index(index, table_name=_SUB_AGENT_RUN)

    sub_cols = _columns(_SUB_AGENT_RUN)
    for name in (
        "agent_run_id",
        "assignment_id",
        "pattern",
        "ordinal",
        "depends_on",
        "optional",
        "objective",
        "artifact",
        "branch_name",
        "merged",
        "token_usage",
        "error",
    ):
        if name in sub_cols:
            op.drop_column(_SUB_AGENT_RUN, name)

    agent_cols = _columns(_AGENT_RUN)
    for name in ("supervision", "pattern", "is_supervisor"):
        if name in agent_cols:
            op.drop_column(_AGENT_RUN, name)
