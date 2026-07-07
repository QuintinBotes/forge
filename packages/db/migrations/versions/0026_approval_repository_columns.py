"""approval repository persistence: requested_actor + escalated columns

Backs the DB-backed ``SqlAlchemyApprovalRepository`` (apps/api) with the two
``approval_request`` columns the domain :class:`forge_approval.models.ApprovalRequest`
carries but the F36 schema (0019) had no home for:

* ``requested_actor`` — the requesting actor reference ("system" | "<kind>:<id>").
  It is what the approval inbox shows and the ``approval.requested`` event carries
  (distinct from the resolvable ``requested_by`` user id), so a faithful repository
  must round-trip it verbatim.
* ``escalated`` — set when a reviewer escalates a gate; persisted on the parent row
  so the single server-side authorizer re-enforces the admin-only rule on every
  subsequent resolve attempt after a reload (not only in-process).

Both are additive, ``NOT NULL`` with a server default, so existing rows read a
sane value ("system" / false) and nothing else in the schema is touched — the
in-memory repository (the unit-test default) is unaffected.

Foundation note (mirrors 0019/0024/0025): ``forge_db``'s metadata is the source
of truth, so a fresh chain already provisions these columns from the model. To
stay idiomatic *and* own an explicit, reversible step this migration is
**idempotent**: ``upgrade`` adds only what is missing, ``downgrade`` drops only
what this revision introduced. Applies cleanly on SQLite (unit path) and the
pgvector Postgres test DB (:5433).

Revision ID: 0026_approval_repository_columns
Revises: 0025_observability_audit_store
Create Date: 2026-07-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)

# revision identifiers, used by Alembic.
revision: str = "0026_approval_repository_columns"
down_revision: str | None = "0025_observability_audit_store"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "approval_request"
# Column names owned by this revision, in add order (downgrade drops in reverse).
_COLUMN_NAMES: tuple[str, ...] = ("requested_actor", "escalated")


def _new_columns() -> list[sa.Column]:
    """Fresh Column objects per call (a Column may be bound to one table only)."""
    return [
        sa.Column(
            "requested_actor",
            sa.String(length=64),
            server_default=sa.text("'system'"),
            nullable=False,
        ),
        sa.Column(
            "escalated",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    ]


def _existing_columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def upgrade() -> None:
    columns = _existing_columns()
    for column in _new_columns():
        if column.name not in columns:
            op.add_column(_TABLE, column)


def downgrade() -> None:
    columns = _existing_columns()
    for name in reversed(_COLUMN_NAMES):
        if name in columns:
            op.drop_column(_TABLE, name)
