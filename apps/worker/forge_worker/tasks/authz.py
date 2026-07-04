"""F30 authz hygiene: purge expired role grants (Celery beat).

Expiry is **authoritative at resolution time** (the resolver ignores any grant
with ``expires_at < now``), so this task is hygiene + audit only: a missed run
never grants stale access. Each run deletes grants whose ``expires_at`` has
passed and writes one ``role_grant.expired`` audit event per deleted grant. A
re-run is idempotent — already-purged grants are gone, so no second event.

The worker writes through ``forge_db.audit``'s chained ``SqlAuditWriter``
directly (it must not import ``forge_api``), so every purge event lands in the
F39 per-workspace hash chain of the shared append-only ``audit_log`` table.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from forge_contracts.audit import AuditEvent
from forge_db.audit.writer import SqlAuditWriter
from forge_db.models import RoleGrant
from forge_db.session import create_session_factory
from forge_worker.celery_app import celery_app

__all__ = ["PURGE_EXPIRED_GRANTS_TASK", "purge_expired_grants", "run_purge"]

PURGE_EXPIRED_GRANTS_TASK = "authz.purge_expired_grants"


def _enum_value(value: object) -> str:
    return value.value if isinstance(value, enum.Enum) else str(value)


def run_purge(session: Session, *, now: datetime | None = None) -> int:
    """Delete expired grants, emitting one ``role_grant.expired`` event each.

    Returns the number of grants purged. Idempotent: a second run over the same
    state finds nothing expired and emits no further events.
    """
    now = now or datetime.now(UTC)
    expired = session.scalars(
        select(RoleGrant).where(RoleGrant.expires_at.is_not(None), RoleGrant.expires_at < now)
    ).all()
    writer = SqlAuditWriter(session)
    for grant in expired:
        writer.emit(
            AuditEvent(
                workspace_id=grant.workspace_id,
                action="role_grant.expired",
                actor_type="system",
                actor_label="system:authz_purge",
                target_type="role_grant",
                target_id=grant.id,
                scope_type=_enum_value(grant.scope_type),
                scope_id=grant.scope_id,
                before={
                    "principal_type": _enum_value(grant.principal_type),
                    "principal_id": str(grant.principal_id),
                    "role": _enum_value(grant.role),
                    "expires_at": grant.expires_at.isoformat() if grant.expires_at else None,
                },
            )
        )
        session.delete(grant)
    session.commit()
    return len(expired)


@celery_app.task(name=PURGE_EXPIRED_GRANTS_TASK)
def purge_expired_grants() -> int:
    """Beat entrypoint: open a DB session and purge expired grants."""
    factory: sessionmaker[Session] = create_session_factory()
    with factory() as session:
        return run_purge(session)
