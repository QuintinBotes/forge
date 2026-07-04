"""F39 audit services: the durable chained ``AuditSink`` + the query surface.

``SqlAuditWriter`` is the canonical durable sink every producer already uses
(F30 authz, F37 auth/provisioning, F33 SSO, F38 cost). F39 re-based it onto
``forge_db.audit.writer.SqlAuditWriter`` — same constructor and ``emit(event)``
call shape, now assigning the per-workspace tamper-evident hash chain and
redacting metadata through F37's canonical :class:`SecretRedactor` before
hashing/persistence (AC4). The write still participates in the caller's
transaction: an audit event and the mutation it records commit atomically —
fail-closed for the security-critical paths.

``AuditService`` backs the admin-only ``/audit`` query API: workspace-isolated
reads, keyset pagination, chain verification, and NDJSON export.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from forge_auth.redaction import SecretRedactor
from forge_contracts.audit import AuditEvent, ChainVerifyResult, canonical_json
from forge_db.audit.chain import verify_chain
from forge_db.audit.repository import AuditQueryRepository
from forge_db.audit.writer import SqlAuditWriter as _ChainedSqlAuditWriter
from forge_db.models import AuditLog

__all__ = ["AuditService", "SqlAuditWriter"]

#: Shared process-wide redactor (stateful known-secret registry lives here).
_DEFAULT_REDACTOR = SecretRedactor()


class SqlAuditWriter(_ChainedSqlAuditWriter):
    """Chained audit writer with the canonical F37 redactor wired by default.

    Keeps the foundation constructor (``SqlAuditWriter(session)``) so every
    existing producer call-site emits into the hash chain unchanged.
    """

    def __init__(
        self,
        session: Session,
        *,
        redactor: object | None = None,
        async_dispatch: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        super().__init__(
            session,
            redactor=redactor if redactor is not None else _DEFAULT_REDACTOR,
            async_dispatch=async_dispatch,
        )


def _entry_payload(row: AuditLog) -> dict[str, Any]:
    """JSON-safe dict of one audit row (API + NDJSON export share this)."""
    return {
        "id": str(row.id),
        "workspace_id": str(row.workspace_id),
        "seq": row.seq,
        "action": row.action,
        "actor_id": str(row.actor_id) if row.actor_id else None,
        "actor_type": row.actor_type,
        "actor_label": row.actor_label,
        "target_type": row.target_type,
        "target_id": str(row.target_id) if row.target_id else None,
        "scope_type": row.scope_type,
        "scope_id": str(row.scope_id) if row.scope_id else None,
        "before": row.before,
        "after": row.after,
        "result": row.result,
        "severity": row.severity,
        "reason": row.reason,
        "details": row.details,
        "detail_ref": row.detail_ref,
        "request_id": row.request_id,
        "payload_hash": row.payload_hash,
        "prev_hash": row.prev_hash,
        "entry_hash": row.entry_hash,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


class AuditService:
    """Workspace-isolated reads + verify + export over the chained audit log."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._repo = AuditQueryRepository(session)

    def list_entries(
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
        return self._repo.list(
            workspace_id,
            actor_id=actor_id,
            actor_type=actor_type,
            action=action,
            target_type=target_type,
            target_id=target_id,
            result=result,
            severity=severity,
            from_time=from_time,
            to_time=to_time,
            q=q,
            cursor=cursor,
            limit=limit,
        )

    def get_entry(self, workspace_id: UUID, entry_id: UUID) -> AuditLog | None:
        """Workspace-isolated single read (foreign id -> ``None`` -> 404)."""
        return self._repo.get(workspace_id, entry_id)

    def verify(
        self,
        workspace_id: UUID,
        *,
        from_seq: int | None = None,
        to_seq: int | None = None,
    ) -> ChainVerifyResult:
        return verify_chain(
            self._session, workspace_id, from_seq=from_seq, to_seq=to_seq
        )

    def export_ndjson(
        self,
        workspace_id: UUID,
        *,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
        actor_label: str | None = None,
    ) -> Iterator[str]:
        """Stream the range as NDJSON (chain fields included — offline
        re-verifiable, AC15) and record an ``audit.exported`` self-event."""
        # Record the export itself BEFORE streaming; the exported range is
        # capped at the self-event's predecessor so the export never contains
        # itself (and never mutates rows).
        exported = SqlAuditWriter(self._session).emit(
            AuditEvent(
                workspace_id=workspace_id,
                action="audit.exported",
                actor_type="user",
                actor_label=actor_label,
                target_type="audit",
                severity="notice",
                details={
                    "from": from_time.isoformat() if from_time else None,
                    "to": to_time.isoformat() if to_time else None,
                },
            )
        )
        self._session.commit()
        cutoff_seq = (exported.seq or 1) - 1

        def _lines() -> Iterator[str]:
            for row in self._repo.iter_export(
                workspace_id, from_time=from_time, to_time=to_time, to_seq=cutoff_seq
            ):
                yield canonical_json(_entry_payload(row)) + "\n"

        return _lines()

    @staticmethod
    def entry_payload(row: AuditLog) -> dict[str, Any]:
        return _entry_payload(row)

    @staticmethod
    def parse_ndjson_line(line: str) -> dict[str, Any]:
        """Convenience for offline auditors/tests: one exported row."""
        return json.loads(line)
