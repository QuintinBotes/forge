"""f36 human-approval-system: generalized approval_request + decision/grant tables

Generalizes the baseline pr-era ``approval_request`` into the canonical F36 gate
frame and creates the two child tables:

* ``approval_request`` gains the polymorphic subject (``subject_type`` /
  ``subject_id``), inbox grouping (``project_id``), risk + SLA (``risk_level``,
  ``expires_at``), multi-approver forward-compat (``required_approvals``), and
  the optional ``context_ref`` snapshot key — all additive and nullable /
  server-defaulted so existing rows are untouched. Two lookup indexes plus the
  partial-unique ``uq_pending_gate`` (at most one open gate of a type per
  subject; generalizes F08's one-pending-pr-per-run).
* ``approval_decision`` — append-only per-approver decision trail, unique per
  ``(approval_request_id, approver_user_id)``, hardened on Postgres with the
  F39 ``attach_immutability_trigger`` BEFORE UPDATE/DELETE block (installed via
  the model's ``after_create`` listener on this ``create_all`` path).
* ``policy_override_grant`` — single-use, short-TTL override grants with the
  partial-unique ``uq_active_override`` (one active grant per
  ``(agent_run_id, action_fingerprint)``).

Foundation note (conform-to-foundation): the gate-type column keeps its
baseline name ``gate`` — it already carries the six-value ``ApprovalGate``
enum, so the slice doc's ``kind``→``gate_type`` rename does not apply. The
``status`` column is stored as plain VARCHAR (no CHECK), so the new
``expired`` value needs no DDL.

Like 0007, this migration is **idempotent** on the baseline-owned table: the
metadata-driven baseline already provisions the new columns/indexes on a fresh
chain, so ``upgrade`` adds only what is missing and ``downgrade`` drops only
what F36 introduced (never ``approval_request`` itself).

Revision ID: 0019_f36_approval_framework
Revises: 0018_f35_benchmark_leaderboard
Create Date: 2026-07-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)
from forge_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0019_f36_approval_framework"
down_revision: str | None = "0018_f35_benchmark_leaderboard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "approval_request"
F36_TABLES = ("approval_decision", "policy_override_grant")

_PENDING_UNIQUE = "uq_pending_gate"
_PENDING_WHERE = "status = 'pending' AND subject_id IS NOT NULL"
_INDEXES = (
    ("ix_approval_request_workspace_status", ["workspace_id", "status"]),
    ("ix_approval_request_project_status", ["project_id", "status"]),
)
_COLUMN_NAMES = (
    "project_id",
    "subject_type",
    "subject_id",
    "required_approvals",
    "risk_level",
    "context_ref",
    "expires_at",
)


def _new_columns() -> list[sa.Column]:
    """Fresh Column objects per call (a Column may be bound to one table only)."""
    return [
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("subject_type", sa.String(length=24), nullable=True),
        sa.Column("subject_id", sa.Uuid(), nullable=True),
        sa.Column(
            "required_approvals", sa.Integer(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column(
            "risk_level", sa.String(length=16), server_default=sa.text("'info'"), nullable=False
        ),
        sa.Column("context_ref", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    ]


def _f36_tables() -> list:
    by_name = {t.name: t for t in Base.metadata.sorted_tables}
    return [by_name[name] for name in F36_TABLES if name in by_name]


def _existing_columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def _existing_indexes() -> set[str]:
    return {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(_TABLE)}


def _existing_tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    columns = _existing_columns()
    for column in _new_columns():
        if column.name not in columns:
            op.add_column(_TABLE, column)

    indexes = _existing_indexes()
    for name, cols in _INDEXES:
        if name not in indexes:
            op.create_index(name, _TABLE, cols)
    if _PENDING_UNIQUE not in indexes:
        op.create_index(
            _PENDING_UNIQUE,
            _TABLE,
            ["subject_type", "subject_id", "gate"],
            unique=True,
            postgresql_where=sa.text(_PENDING_WHERE),
            sqlite_where=sa.text(_PENDING_WHERE),
        )

    # Backfill: pre-F36 rows are all pr-era workflow gates — key them by their
    # workflow run so the polymorphic subject is populated for the unique index.
    op.execute(
        sa.text(
            f"UPDATE {_TABLE} SET subject_type = 'workflow_run', "
            "subject_id = workflow_run_id "
            "WHERE subject_type IS NULL AND workflow_run_id IS NOT NULL"
        )
    )

    existing = _existing_tables()
    tables = [t for t in _f36_tables() if t.name not in existing]
    if tables:
        Base.metadata.create_all(bind=op.get_bind(), tables=tables)


def downgrade() -> None:
    existing = _existing_tables()
    for table in reversed(_f36_tables()):
        if table.name in existing:
            table.drop(bind=op.get_bind())

    indexes = _existing_indexes()
    if _PENDING_UNIQUE in indexes:
        op.drop_index(_PENDING_UNIQUE, table_name=_TABLE)
    for name, _cols in _INDEXES:
        if name in indexes:
            op.drop_index(name, table_name=_TABLE)

    columns = _existing_columns()
    for name in reversed(_COLUMN_NAMES):
        if name in columns:
            op.drop_column(_TABLE, name)
