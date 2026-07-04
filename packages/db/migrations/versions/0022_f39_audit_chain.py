"""f39 audit-log: hash chain on audit_log + audit_chain_head cursor

Extends F30's ``audit_log`` **in place** (slice §3.1) with the tamper-evident
chain columns (``seq``/``payload_hash``/``prev_hash``/``entry_hash``) and the
observability columns (``actor_label``/``severity``/``reason``/``detail_ref``/
``request_id``), creates the per-workspace ``audit_chain_head`` cursor table,
adds the filter indexes, and **backfills** the chain over any pre-F39 rows
(ordered ``created_at, id`` per workspace; AC20) so ``verify_chain`` accepts
history and live appends continue seamlessly.

Idempotent on a fresh metadata-driven chain (0012 already created the extended
``audit_log``, 0001 the ``audit_chain_head`` — every step existence-guarded,
mirroring 0019/0020/0021). On Postgres the F30-era ``audit_log_immutable``
trigger is temporarily disabled around the backfill UPDATE (the one sanctioned
mutation, performed by the migration itself) and re-enabled afterwards.

Revision ID: 0022_f39_audit_chain
Revises: 0021_f38_cost_ledger
Create Date: 2026-07-04
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)
from forge_contracts.audit import GENESIS_HASH, compute_entry_hash, compute_payload_hash
from forge_db.base import Base, json_type

# revision identifiers, used by Alembic.
revision: str = "0022_f39_audit_chain"
down_revision: str | None = "0021_f38_cost_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AUDIT = "audit_log"
_HEAD = "audit_chain_head"

def _new_columns() -> list[sa.Column]:
    """Fresh Column objects per call (a Column can only bind to one table)."""
    return [
        sa.Column("seq", sa.BigInteger(), nullable=True),
        sa.Column("actor_label", sa.String(255), nullable=True),
        sa.Column("severity", sa.String(16), nullable=False, server_default="info"),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("payload_hash", sa.String(64), nullable=True),
        sa.Column("prev_hash", sa.String(64), nullable=True),
        sa.Column("entry_hash", sa.String(64), nullable=True),
        sa.Column("detail_ref", json_type(), nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
    ]


_NEW_INDEXES: tuple[tuple[str, tuple[str, ...], bool], ...] = (
    ("uq_audit_log_workspace_seq", ("workspace_id", "seq"), True),
    ("ix_audit_log_actor", ("workspace_id", "actor_id"), False),
    ("ix_audit_log_target", ("workspace_id", "target_type", "target_id"), False),
)


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _columns(table: str) -> set[str]:
    return {c["name"] for c in _inspector().get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {i["name"] for i in _inspector().get_indexes(table)}


def _set_pg_trigger(enabled: bool) -> None:
    """Disable/enable F30's ``audit_log_immutable`` trigger around the backfill."""
    if op.get_bind().dialect.name != "postgresql":
        return
    verb = "ENABLE" if enabled else "DISABLE"
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE t.tgname = 'audit_log_immutable' AND c.relname = 'audit_log') THEN "
            f"EXECUTE 'ALTER TABLE audit_log {verb} TRIGGER audit_log_immutable'; "
            "END IF; END $$;"
        )
    )


def _as_uuid(value: object) -> uuid.UUID:
    """Coerce a DB-returned id (UUID on Postgres, hex str on SQLite) to a UUID."""
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _backfill_chain(bind: sa.engine.Connection) -> None:
    """Assign gap-free ``seq`` + hashes to every unchained row, per workspace."""
    audit = Base.metadata.tables[_AUDIT]
    head = Base.metadata.tables[_HEAD]

    # Current cursor per workspace (rows may already be chained on re-runs).
    heads: dict[uuid.UUID, tuple[int, str]] = {
        _as_uuid(row.workspace_id): (row.last_seq, row.last_hash)
        for row in bind.execute(sa.select(head)).fetchall()
    }

    unchained = bind.execute(
        sa.select(audit)
        .where(audit.c.seq.is_(None))
        .order_by(audit.c.workspace_id, audit.c.created_at, audit.c.id)
    ).fetchall()

    touched: set[uuid.UUID] = set()
    for row in unchained:
        ws = _as_uuid(row.workspace_id)
        last_seq, last_hash = heads.get(ws, (0, GENESIS_HASH))
        seq = last_seq + 1
        payload_hash = compute_payload_hash(
            {"before": row.before, "after": row.after, "details": row.details or {}}
        )
        entry_hash = compute_entry_hash(
            prev_hash=last_hash,
            workspace_id=ws,
            seq=seq,
            occurred_at=row.created_at,
            actor_type=row.actor_type,
            actor_id=_as_uuid(row.actor_id) if row.actor_id is not None else None,
            actor_label=row.actor_label,
            action=row.action,
            target_type=row.target_type,
            target_id=_as_uuid(row.target_id) if row.target_id is not None else None,
            scope_type=row.scope_type,
            scope_id=_as_uuid(row.scope_id) if row.scope_id is not None else None,
            result=row.result,
            payload_hash=payload_hash,
        )
        values = {
            "seq": seq,
            "payload_hash": payload_hash,
            "prev_hash": last_hash,
            "entry_hash": entry_hash,
        }
        result = bind.execute(
            audit.update().where(audit.c.id == row.id).values(**values)
        )
        if result.rowcount == 0:
            # SQLite stores ORM-written UUIDs as undashed hex; rows inserted by
            # raw SQL may carry the dashed form instead. Match it explicitly so
            # no row is left unchained while the head cursor advances.
            bind.execute(
                sa.text(
                    "UPDATE audit_log SET seq = :seq, payload_hash = :payload_hash, "
                    "prev_hash = :prev_hash, entry_hash = :entry_hash WHERE id = :id"
                ),
                {**values, "id": str(_as_uuid(row.id))},
            )
        heads[ws] = (seq, entry_hash)
        touched.add(ws)

    now = sa.func.now()
    for ws in touched:
        last_seq, last_hash = heads[ws]
        existing = bind.execute(
            sa.select(head.c.id).where(head.c.workspace_id == ws)
        ).fetchone()
        if existing is None:
            bind.execute(
                head.insert().values(
                    id=uuid.uuid4(),
                    workspace_id=ws,
                    last_seq=last_seq,
                    last_hash=last_hash,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            bind.execute(
                head.update()
                .where(head.c.workspace_id == ws)
                .values(last_seq=last_seq, last_hash=last_hash)
            )


def upgrade() -> None:
    bind = op.get_bind()

    existing_cols = _columns(_AUDIT)
    for column in _new_columns():
        if column.name not in existing_cols:
            op.add_column(_AUDIT, column)

    if _HEAD not in set(_inspector().get_table_names()):
        Base.metadata.create_all(bind=bind, tables=[Base.metadata.tables[_HEAD]])

    existing_idx = _indexes(_AUDIT)
    for name, cols, unique in _NEW_INDEXES:
        if name not in existing_idx:
            op.create_index(name, _AUDIT, list(cols), unique=unique)

    _set_pg_trigger(enabled=False)
    try:
        _backfill_chain(bind)
    finally:
        _set_pg_trigger(enabled=True)


def downgrade() -> None:
    bind = op.get_bind()

    existing_idx = _indexes(_AUDIT)
    for name, _cols, _unique in _NEW_INDEXES:
        if name in existing_idx:
            op.drop_index(name, table_name=_AUDIT)

    existing_cols = _columns(_AUDIT)
    for column in _new_columns():
        if column.name in existing_cols:
            op.drop_column(_AUDIT, column.name)

    if _HEAD in set(_inspector().get_table_names()):
        Base.metadata.drop_all(bind=bind, tables=[Base.metadata.tables[_HEAD]])
