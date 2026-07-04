"""Hash-chain verification for the ``audit_log`` table (F39 tamper detection).

Re-walks a workspace's chained rows in ``seq`` order, recomputing
``payload_hash`` and ``entry_hash`` from the stored field values and asserting
each row links to its predecessor. Any out-of-band mutation, mid-chain
deletion, or (via the ``audit_chain_head`` cross-check) tail truncation is
reported with the first broken sequence number.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from forge_contracts.audit import (
    GENESIS_HASH,
    ChainVerifyResult,
    compute_entry_hash,
    compute_payload_hash,
)
from forge_db.models.audit import AuditChainHead, AuditLog

__all__ = ["verify_chain"]


def verify_chain(
    session: Session,
    workspace_id: UUID,
    *,
    from_seq: int | None = None,
    to_seq: int | None = None,
) -> ChainVerifyResult:
    """Verify one workspace's audit chain (optionally a ``seq`` range).

    Checks, per row: contiguity of ``seq``, ``prev_hash`` linkage, and that the
    stored ``payload_hash``/``entry_hash`` match a recomputation over the stored
    values. For a full walk (no ``to_seq``) the ``audit_chain_head`` cursor is
    cross-checked so deleting the chain *tail* is also detected.
    """

    def broken(seq: int | None, entries: int, detail: str) -> ChainVerifyResult:
        return ChainVerifyResult(
            workspace_id=workspace_id,
            ok=False,
            entries_checked=entries,
            broken_at_seq=seq,
            detail=detail,
        )

    query = (
        select(AuditLog)
        .where(AuditLog.workspace_id == workspace_id, AuditLog.seq.is_not(None))
        .order_by(AuditLog.seq)
    )
    if from_seq is not None:
        query = query.where(AuditLog.seq >= from_seq)
    if to_seq is not None:
        query = query.where(AuditLog.seq <= to_seq)
    rows = session.scalars(query).all()

    # Anchor: GENESIS for a walk from the start; otherwise the predecessor row.
    start = from_seq if from_seq is not None and from_seq > 1 else 1
    if start == 1:
        prev_hash = GENESIS_HASH
    else:
        anchor = session.scalars(
            select(AuditLog).where(
                AuditLog.workspace_id == workspace_id, AuditLog.seq == start - 1
            )
        ).one_or_none()
        if anchor is None or anchor.entry_hash is None:
            return broken(start - 1, 0, f"anchor row seq={start - 1} missing")
        prev_hash = anchor.entry_hash

    expected_seq = start
    checked = 0
    for row in rows:
        if row.seq != expected_seq:
            detail = f"gap: expected seq {expected_seq}, found {row.seq}"
            return broken(expected_seq, checked, detail)
        if row.prev_hash != prev_hash:
            return broken(row.seq, checked, f"prev_hash mismatch at seq {row.seq}")
        recomputed_payload = compute_payload_hash(
            {"before": row.before, "after": row.after, "details": row.details}
        )
        if row.payload_hash != recomputed_payload:
            return broken(row.seq, checked, f"payload_hash mismatch at seq {row.seq}")
        recomputed_entry = compute_entry_hash(
            prev_hash=prev_hash,
            workspace_id=row.workspace_id,
            seq=row.seq,
            occurred_at=row.created_at,
            actor_type=row.actor_type,
            actor_id=row.actor_id,
            actor_label=row.actor_label,
            action=row.action,
            target_type=row.target_type,
            target_id=row.target_id,
            scope_type=row.scope_type,
            scope_id=row.scope_id,
            result=row.result,
            payload_hash=row.payload_hash or "",
        )
        if row.entry_hash != recomputed_entry:
            return broken(row.seq, checked, f"entry_hash mismatch at seq {row.seq}")
        prev_hash = row.entry_hash
        expected_seq += 1
        checked += 1

    # Full-walk only: the head cursor must agree with the last surviving row —
    # otherwise the chain tail was deleted (rows N..last vanished).
    if to_seq is None:
        head = session.scalars(
            select(AuditChainHead).where(AuditChainHead.workspace_id == workspace_id)
        ).one_or_none()
        if head is not None:
            last_seq = expected_seq - 1
            last_hash = prev_hash
            if head.last_seq != last_seq or (last_seq > 0 and head.last_hash != last_hash):
                return broken(
                    head.last_seq,
                    checked,
                    f"head cursor at seq {head.last_seq} but chain ends at {last_seq}",
                )

    return ChainVerifyResult(
        workspace_id=workspace_id, ok=True, entries_checked=checked, broken_at_seq=None
    )
