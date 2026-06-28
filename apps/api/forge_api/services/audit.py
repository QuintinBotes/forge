"""``SqlAuditWriter`` — the durable :class:`AuditSink` backing F30 (F39 sliver).

Persists :class:`forge_contracts.audit.AuditEvent` records into the shared
append-only ``audit_log`` table. The table is hardened against UPDATE/DELETE on
Postgres (BEFORE trigger); this writer only ever inserts.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from forge_contracts.audit import AuditEvent
from forge_db.models import AuditLog


class SqlAuditWriter:
    """Writes audit events to the ``audit_log`` table on a shared session.

    The write participates in the caller's transaction (the service flushes/
    commits) so an audit event and the mutation it records commit atomically —
    no authz mutation path can skip its audit record.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def emit(self, event: AuditEvent) -> None:
        row = AuditLog(
            workspace_id=event.workspace_id,
            action=event.action,
            actor_id=event.actor_id,
            actor_type=event.actor_type,
            target_type=event.target_type,
            target_id=event.target_id,
            scope_type=event.scope_type,
            scope_id=event.scope_id,
            before=event.before,
            after=event.after,
            result=event.result,
            details=event.details,
        )
        self._session.add(row)
        self._session.flush()


__all__ = ["SqlAuditWriter"]
