"""F39 audit worker tasks: async sink + scheduled chain verifier.

- ``audit.record`` — the fail-open async ``AuditSink`` half: rebuilds the
  serialized :class:`AuditEvent` and persists it through the chained
  ``SqlAuditWriter`` on its own session. Celery retries transient failures;
  a terminal failure is logged (never raised into the producing operation).
- ``audit.verify_chain_all`` — Celery beat (daily by default): re-walks every
  workspace's hash chain; a break records a ``system``/``critical``
  ``audit.chain_broken`` event (Journey C) and logs at CRITICAL.

The worker writes through ``forge_db.audit`` directly (mirroring
``tasks/authz.py``: no ``forge_api`` import) with F37's canonical
``SecretRedactor``. The slice's MinIO ``audit.archive`` job is PARKED — no
object-store client exists in-tree yet; the streaming ``GET /audit/export``
endpoint covers the export surface.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from forge_auth.redaction import SecretRedactor
from forge_contracts.audit import AuditEvent, ChainVerifyResult
from forge_db.audit.chain import verify_chain
from forge_db.audit.writer import SqlAuditWriter
from forge_db.models import AuditChainHead
from forge_db.session import create_session_factory
from forge_worker.celery_app import celery_app

__all__ = [
    "AUDIT_RECORD_TASK",
    "AUDIT_VERIFY_TASK",
    "record_audit_event",
    "run_record",
    "run_verify_all",
    "verify_chain_all",
]

logger = logging.getLogger("forge.audit")

AUDIT_RECORD_TASK = "audit.record"
AUDIT_VERIFY_TASK = "audit.verify_chain_all"

_redactor = SecretRedactor()


def run_record(session: Session, payload: dict[str, Any]) -> None:
    """Rebuild the event and persist it into the chain (commits)."""
    event = AuditEvent.model_validate(payload)
    SqlAuditWriter(session, redactor=_redactor).emit(event)
    session.commit()


def run_verify_all(session: Session) -> dict[str, ChainVerifyResult]:
    """Verify every workspace chain; audit + alarm on any break (AC16)."""
    workspace_ids = session.scalars(select(AuditChainHead.workspace_id)).all()
    results: dict[str, ChainVerifyResult] = {}
    for ws_id in workspace_ids:
        result = verify_chain(session, ws_id)
        results[str(ws_id)] = result
        if not result.ok:
            logger.critical(
                "audit chain BROKEN for workspace %s at seq %s: %s",
                ws_id,
                result.broken_at_seq,
                result.detail,
            )
            SqlAuditWriter(session, redactor=_redactor).emit(
                AuditEvent(
                    workspace_id=UUID(str(ws_id)),
                    action="audit.chain_broken",
                    actor_type="system",
                    actor_label="system:audit_verifier",
                    target_type="audit",
                    result="error",
                    severity="critical",
                    reason=result.detail,
                    details={
                        "broken_at_seq": result.broken_at_seq,
                        "entries_checked": result.entries_checked,
                    },
                )
            )
            session.commit()
    return results


@celery_app.task(name=AUDIT_RECORD_TASK, bind=True, max_retries=5, default_retry_delay=5)
def record_audit_event(self, payload: dict[str, Any]) -> None:  # type: ignore[no-untyped-def]
    """Async-sink entrypoint: persist one serialized audit event (fail-open)."""
    factory: sessionmaker[Session] = create_session_factory()
    try:
        with factory() as session:
            run_record(session, payload)
    except Exception as exc:
        try:
            raise self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            logger.error("audit event dropped after retries: %s", payload.get("action"))


@celery_app.task(name=AUDIT_VERIFY_TASK)
def verify_chain_all() -> dict[str, bool]:
    """Beat entrypoint: verify all workspace chains; returns per-ws verdicts."""
    factory: sessionmaker[Session] = create_session_factory()
    with factory() as session:
        results = run_verify_all(session)
    return {ws: r.ok for ws, r in results.items()}
