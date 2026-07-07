"""Workspace-scoped, keyset-paginated audit reads (F39 query surface).

Deliberately exposes **no update/delete path** (AC7): the only write into
``audit_log`` is ``SqlAuditWriter.emit``, and the ORM-level guards in
``forge_db.models.audit`` reject any mutation flush on every dialect.

Pagination is keyset on ``seq`` (newest first): the opaque cursor is the
url-safe base64 of the last row's ``seq``, so pages are gapless and
duplicate-free even while new rows are appended.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterator
from datetime import datetime
from uuid import UUID

from sqlalchemy import String, cast, or_, select
from sqlalchemy.orm import Session

from forge_db.models.audit import AuditLog

__all__ = ["AuditQueryRepository", "decode_cursor", "encode_cursor"]


def encode_cursor(seq: int) -> str:
    """Opaque keyset cursor for the row with chain position ``seq``."""
    return base64.urlsafe_b64encode(str(seq).encode("ascii")).decode("ascii")


def decode_cursor(cursor: str) -> int | None:
    """Decode a cursor; returns ``None`` for anything malformed (fresh page)."""
    try:
        return int(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("ascii"))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None


class AuditQueryRepository:
    """Read-only repository over the chained ``audit_log`` table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, workspace_id: UUID, entry_id: UUID) -> AuditLog | None:
        """One entry, workspace-isolated: a foreign id resolves to ``None``."""
        return self._session.scalars(
            select(AuditLog).where(AuditLog.workspace_id == workspace_id, AuditLog.id == entry_id)
        ).one_or_none()

    def list(
        self,
        workspace_id: UUID,
        *,
        actor_id: UUID | None = None,
        actor_type: str | None = None,
        action: list[str] | None = None,
        target_type: str | None = None,
        target_id: UUID | None = None,
        result: str | None = None,
        severity: str | None = None,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
        q: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> tuple[list[AuditLog], str | None]:
        """Filtered, keyset-paginated page (newest first) + the next cursor."""
        query = select(AuditLog).where(AuditLog.workspace_id == workspace_id)
        if actor_id is not None:
            query = query.where(AuditLog.actor_id == actor_id)
        if actor_type is not None:
            query = query.where(AuditLog.actor_type == actor_type)
        if action:
            query = query.where(AuditLog.action.in_(action))
        if target_type is not None:
            query = query.where(AuditLog.target_type == target_type)
        if target_id is not None:
            query = query.where(AuditLog.target_id == target_id)
        if result is not None:
            query = query.where(AuditLog.result == result)
        if severity is not None:
            query = query.where(AuditLog.severity == severity)
        if from_time is not None:
            query = query.where(AuditLog.created_at >= from_time)
        if to_time is not None:
            query = query.where(AuditLog.created_at <= to_time)
        if q:
            needle = f"%{q}%"
            query = query.where(
                or_(
                    AuditLog.action.like(needle),
                    AuditLog.reason.like(needle),
                    AuditLog.actor_label.like(needle),
                    cast(AuditLog.details, String).like(needle),
                )
            )

        after_seq = decode_cursor(cursor) if cursor else None
        if after_seq is not None:
            query = query.where(AuditLog.seq < after_seq)

        query = query.order_by(AuditLog.seq.desc().nulls_last()).limit(limit + 1)
        rows = list(self._session.scalars(query).all())

        next_cursor: str | None = None
        if len(rows) > limit:
            rows = rows[:limit]
            last_seq = rows[-1].seq
            # Legacy unchained rows (seq NULL) sort last; pagination ends there.
            if last_seq is not None:
                next_cursor = encode_cursor(last_seq)
        return rows, next_cursor

    def iter_export(
        self,
        workspace_id: UUID,
        *,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
        to_seq: int | None = None,
        batch_size: int = 500,
    ) -> Iterator[AuditLog]:
        """Yield rows oldest-first for NDJSON export (chain-order, streamed)."""
        after_seq = 0
        while True:
            query = (
                select(AuditLog)
                .where(
                    AuditLog.workspace_id == workspace_id,
                    AuditLog.seq.is_not(None),
                    AuditLog.seq > after_seq,
                )
                .order_by(AuditLog.seq)
                .limit(batch_size)
            )
            if to_seq is not None:
                query = query.where(AuditLog.seq <= to_seq)
            if from_time is not None:
                query = query.where(AuditLog.created_at >= from_time)
            if to_time is not None:
                query = query.where(AuditLog.created_at <= to_time)
            rows = self._session.scalars(query).all()
            if not rows:
                return
            yield from rows
            last = rows[-1].seq
            assert last is not None  # filtered above
            after_seq = last
