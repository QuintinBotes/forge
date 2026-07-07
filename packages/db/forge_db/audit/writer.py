"""``SqlAuditWriter`` — the durable, chained :class:`AuditSink` (F39).

Persists :class:`forge_contracts.audit.AuditEvent` records into the shared
append-only ``audit_log`` table, assigning each row its position in the
per-workspace tamper-evident hash chain.

``emit`` algorithm (synchronous, on the **caller's** session/transaction — so
security-critical events commit atomically with the action they record,
fail-closed):

1. redact ``before``/``after``/``details``/``reason`` through the injected
   redactor (F37's ``SecretRedactor`` in production wiring),
2. lock the workspace's ``audit_chain_head`` row ``FOR UPDATE`` (inserting the
   genesis head when absent) — serializing concurrent appends,
3. assign ``seq = last_seq + 1`` / ``prev_hash = last_hash``, compute
   ``payload_hash`` + ``entry_hash`` (pure helpers from the contract),
4. INSERT the ``audit_log`` row and advance the head.

``emit_async`` is the fail-open path for non-critical, high-volume events: it
serializes the event and hands it to the injected dispatcher (production: the
``audit.record`` Celery task). Dispatcher failures are swallowed after a
best-effort synchronous fallback — routine observability must never become a
liveness risk for the agent loop.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forge_contracts.audit import (
    GENESIS_HASH,
    AuditEvent,
    compute_entry_hash,
    compute_payload_hash,
)
from forge_db.audit.redaction import redact_metadata
from forge_db.models.audit import AuditChainHead, AuditLog

__all__ = ["SqlAuditWriter"]

logger = logging.getLogger("forge.audit")


class SqlAuditWriter:
    """Writes chained audit events to ``audit_log`` on a shared session.

    The write participates in the caller's transaction (the service flushes/
    commits) so an audit event and the mutation it records commit atomically —
    no mutation path can skip its audit record (fail-closed).
    """

    def __init__(
        self,
        session: Session,
        *,
        redactor: object | None = None,
        async_dispatch: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._session = session
        self._redactor = redactor
        self._async_dispatch = async_dispatch

    # ------------------------------------------------------------------ emit #

    def emit(self, event: AuditEvent) -> AuditLog:
        """Redact, chain, and INSERT one audit row; returns the ORM row."""
        session = self._session
        before = redact_metadata(event.before, self._redactor)
        after = redact_metadata(event.after, self._redactor)
        details = redact_metadata(event.details or {}, self._redactor)
        reason = redact_metadata(event.reason, self._redactor)

        head = session.execute(
            select(AuditChainHead)
            .where(AuditChainHead.workspace_id == event.workspace_id)
            .with_for_update()
        ).scalar_one_or_none()
        if head is None:
            head = AuditChainHead(
                workspace_id=event.workspace_id, last_seq=0, last_hash=GENESIS_HASH
            )
            session.add(head)
            session.flush()

        seq = head.last_seq + 1
        prev_hash = head.last_hash
        occurred_at = event.created_at or datetime.now(UTC)
        payload_hash = compute_payload_hash({"before": before, "after": after, "details": details})
        entry_hash = compute_entry_hash(
            prev_hash=prev_hash,
            workspace_id=event.workspace_id,
            seq=seq,
            occurred_at=occurred_at,
            actor_type=event.actor_type,
            actor_id=event.actor_id,
            actor_label=event.actor_label,
            action=event.action,
            target_type=event.target_type,
            target_id=event.target_id,
            scope_type=event.scope_type,
            scope_id=event.scope_id,
            result=event.result,
            payload_hash=payload_hash,
        )

        row = AuditLog(
            workspace_id=event.workspace_id,
            action=event.action,
            actor_id=event.actor_id,
            actor_type=event.actor_type,
            actor_label=event.actor_label,
            target_type=event.target_type,
            target_id=event.target_id,
            scope_type=event.scope_type,
            scope_id=event.scope_id,
            before=before,
            after=after,
            result=event.result,
            severity=event.severity,
            reason=reason,
            details=details,
            detail_ref=dict(event.detail_ref) if event.detail_ref else None,
            request_id=event.request_id,
            seq=seq,
            payload_hash=payload_hash,
            prev_hash=prev_hash,
            entry_hash=entry_hash,
            created_at=occurred_at,
        )
        session.add(row)
        head.last_seq = seq
        head.last_hash = entry_hash
        session.flush()
        return row

    # ------------------------------------------------------- emit_async ---- #

    def emit_async(self, event: AuditEvent) -> None:
        """Fail-open enqueue for non-critical events (never raises).

        Hands the serialized event to the injected dispatcher (production: the
        ``audit.record`` Celery task). Without a dispatcher — or when dispatch
        fails — falls back to a best-effort synchronous ``emit``; any residual
        failure is logged and dropped, never raised into the producing
        operation.
        """
        payload = event.model_dump(mode="json")
        if self._async_dispatch is not None:
            try:
                self._async_dispatch(payload)
                return
            except Exception:
                logger.warning(
                    "audit async dispatch failed; falling back to sync emit",
                    exc_info=True,
                )
        try:
            self.emit(event)
        except Exception:
            logger.error("audit event dropped (fail-open path)", exc_info=True)
