"""hard13 envelope key version: api_key.key_version + rotated_at (+ index).

Additive, data-preserving, reversible. HARD-13 makes ``api_key`` (the
production-backed BYOK vault store) envelope-encryption aware:

* ``key_version SMALLINT NOT NULL DEFAULT 1`` — the KEK version the row's data
  key is currently wrapped under, so KEK rotation can target
  ``WHERE key_version < :current`` cheaply.
* ``rotated_at timestamptz NULL`` — when the row's DEK was last re-wrapped
  (rotation audit trail).
* ``ix_api_key_key_version`` — index for the rotation query above.

The wrapped data key travels *inside* ``encrypted_secret`` (a self-describing
envelope blob), so no separate DEK column is needed; ``key_version`` is
denormalised purely to make rotation index-friendly. ``expires_at`` already
exists (no schema change) — HARD-13 makes it enforced in code.

Existence-guarded like 0017/0016 so a fresh metadata-driven install (which
already carries the columns) and an upgraded DB converge. Downgrade drops the
index and both columns. NOTE: rows written by the envelope cipher remain
decryptable after a downgrade only while the code still understands the envelope
blob format — a schema downgrade must be paired with a code rollback (captured in
docs/self-hosting/security.md).

Revision ID: 0023_envelope_key_version
Revises: 0022_f39_audit_chain
Create Date: 2026-07-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0023_envelope_key_version"
down_revision: str | None = "0022_f39_audit_chain"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "api_key"
_INDEX = "ix_api_key_key_version"


def _columns() -> tuple[sa.Column, ...]:
    """Fresh Column objects per call (a Column binds to one table only)."""
    return (
        sa.Column(
            "key_version",
            sa.SmallInteger(),
            nullable=False,
            server_default="1",
        ),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
    )


def _existing_columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def _existing_indexes() -> set[str]:
    return {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(_TABLE)}


def upgrade() -> None:
    columns = _existing_columns()
    for column in _columns():
        if column.name not in columns:
            op.add_column(_TABLE, column)
    if _INDEX not in _existing_indexes():
        op.create_index(_INDEX, _TABLE, ["key_version"])


def downgrade() -> None:
    if _INDEX in _existing_indexes():
        op.drop_index(_INDEX, table_name=_TABLE)
    columns = _existing_columns()
    for column in reversed(_columns()):
        if column.name in columns:
            op.drop_column(_TABLE, column.name)
