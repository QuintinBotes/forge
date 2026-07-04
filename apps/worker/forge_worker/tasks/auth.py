"""F37 auth hygiene: purge expired platform API keys (Celery beat).

Expiry is **authoritative at verify time** (an expired key never
authenticates, whether or not this task has run), so this task is hygiene +
audit only — a missed run never grants stale access. Each run revokes
``platform_api_key`` rows whose ``expires_at`` has passed (``revoked_at =
expires_at``, rows kept for audit, never deleted) and writes one
``apikey.expired`` audit event per key. Re-runs are idempotent: already
revoked rows are skipped and emit no second event.

Specifically covers Security "automatic expiry for agent tokens" for
``kind=agent_runner`` keys (which the ``platform_api_key`` CHECK forces to
carry ``expires_at``).

The worker writes :class:`AuditLog` rows directly (it must not import
``forge_api``), mirroring ``forge_worker.tasks.authz``; the row schema matches
the shared append-only ``audit_log`` table owned by cross-cutting/F39.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from forge_db.models import AuditLog, PlatformAPIKey
from forge_db.session import create_session_factory
from forge_worker.celery_app import celery_app

__all__ = ["PURGE_EXPIRED_KEYS_TASK", "purge_expired_keys", "run_purge_expired_keys"]

PURGE_EXPIRED_KEYS_TASK = "auth.purge_expired_keys"


def _enum_value(value: object) -> str:
    return value.value if isinstance(value, enum.Enum) else str(value)


def run_purge_expired_keys(session: Session, *, now: datetime | None = None) -> int:
    """Revoke expired, not-yet-revoked platform keys; one audit event each.

    Returns the number of keys revoked. Idempotent: revoked rows are excluded
    from the query, so a second run finds nothing and emits no further events.
    """
    now = now or datetime.now(UTC)
    expired = session.scalars(
        select(PlatformAPIKey).where(
            PlatformAPIKey.expires_at.is_not(None),
            PlatformAPIKey.expires_at < now,
            PlatformAPIKey.revoked_at.is_(None),
        )
    ).all()
    for key in expired:
        key.revoked_at = key.expires_at
        session.add(
            AuditLog(
                workspace_id=key.workspace_id,
                action="apikey.expired",
                actor_type="system",
                target_type="platform_api_key",
                target_id=key.id,
                # Metadata is prefix/kind/role only — never the hash or a token.
                before={
                    "name": key.name,
                    "key_prefix": key.key_prefix,
                    "kind": _enum_value(key.kind),
                    "role": _enum_value(key.role),
                    "expires_at": key.expires_at.isoformat() if key.expires_at else None,
                },
            )
        )
    session.commit()
    return len(expired)


@celery_app.task(name=PURGE_EXPIRED_KEYS_TASK)
def purge_expired_keys() -> int:
    """Beat entrypoint: open a DB session and purge expired platform keys."""
    factory: sessionmaker[Session] = create_session_factory()
    with factory() as session:
        return run_purge_expired_keys(session)
