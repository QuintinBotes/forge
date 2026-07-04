"""F36 approval worker tasks (Celery beat + the grant-consumption contract).

Two DB-backed operations against the canonical F36 tables:

* :func:`run_expire_sweep` — the SLA sweeper: pending ``approval_request`` rows
  past ``expires_at`` flip to ``expired`` (a terminal state; the subject's
  workflow routes to ``needs_human_input`` downstream). One immutable
  ``approval.expired`` audit row is written per swept gate; a re-run is
  idempotent.
* :func:`run_consume_grant` — the *consumption* side of J5, implementing the
  frozen ``PolicyOverrideGate.consume`` contract for the agent-runtime resume
  path: atomically check-and-consume a non-expired, unconsumed
  ``policy_override_grant`` bound to the exact ``(agent_run_id,
  action_fingerprint)``. ``True`` allows exactly one call; any mismatch,
  expiry, or prior consumption denies. The single UPDATE-where-active
  statement makes double-consumption impossible even across workers.

Pure session-in functions (the authz-purge pattern) so tests run on hermetic
SQLite; the Celery entrypoints open a session from the default factory.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from forge_db.models import ApprovalRequest, ApprovalStatus, AuditLog, PolicyOverrideGrant
from forge_db.session import create_session_factory
from forge_worker.celery_app import celery_app

__all__ = [
    "CONSUME_OVERRIDE_GRANT_TASK",
    "EXPIRE_APPROVALS_TASK",
    "consume_override_grant",
    "expire_pending_approvals",
    "run_consume_grant",
    "run_expire_sweep",
]

EXPIRE_APPROVALS_TASK = "approvals.expire_pending"
CONSUME_OVERRIDE_GRANT_TASK = "approvals.consume_override_grant"


def run_expire_sweep(session: Session, *, now: datetime | None = None) -> int:
    """Mark overdue pending gates ``expired``; returns the number swept.

    Idempotent: swept gates are no longer ``pending``, so a second run finds
    nothing and writes no further audit rows.
    """
    now = now or datetime.now(UTC)
    overdue = session.scalars(
        select(ApprovalRequest).where(
            ApprovalRequest.status == ApprovalStatus.PENDING,
            ApprovalRequest.expires_at.is_not(None),
            ApprovalRequest.expires_at < now,
        )
    ).all()
    for request in overdue:
        request.status = ApprovalStatus.EXPIRED
        request.decided_at = now
        session.add(
            AuditLog(
                workspace_id=request.workspace_id,
                action="approval.expired",
                actor_type="system",
                target_type="approval_request",
                target_id=request.id,
                details={
                    "gate": request.gate.value,
                    "expired_at": now.isoformat(),
                    "follow_up_state": "needs_human_input",
                },
            )
        )
    session.commit()
    return len(overdue)


def run_consume_grant(
    session: Session,
    *,
    agent_run_id: uuid.UUID,
    action_fingerprint: str,
    now: datetime | None = None,
) -> bool:
    """Atomically consume the active grant for this exact action, if any.

    Returns ``True`` (allow this single call) only when a matching,
    unconsumed, unexpired grant existed and was flipped by THIS statement —
    never granting future scope (Build-Prompt constraint #2).
    """
    now = now or datetime.now(UTC)
    result = session.execute(
        update(PolicyOverrideGrant)
        .where(
            PolicyOverrideGrant.agent_run_id == agent_run_id,
            PolicyOverrideGrant.action_fingerprint == action_fingerprint,
            PolicyOverrideGrant.consumed.is_(False),
            PolicyOverrideGrant.expires_at > now,
        )
        .values(consumed=True)
    )
    consumed = (result.rowcount or 0) > 0
    if consumed:
        grant = session.scalars(
            select(PolicyOverrideGrant).where(
                PolicyOverrideGrant.agent_run_id == agent_run_id,
                PolicyOverrideGrant.action_fingerprint == action_fingerprint,
                PolicyOverrideGrant.consumed.is_(True),
            )
        ).first()
        if grant is not None:
            session.add(
                AuditLog(
                    workspace_id=grant.workspace_id,
                    action="policy_override.consumed",
                    actor_type="system",
                    target_type="policy_override_grant",
                    target_id=grant.id,
                    details={"agent_run_id": str(agent_run_id)},
                )
            )
    session.commit()
    return consumed


@celery_app.task(name=EXPIRE_APPROVALS_TASK)
def expire_pending_approvals() -> int:
    """Beat entrypoint: open a DB session and sweep overdue pending gates."""
    factory: sessionmaker[Session] = create_session_factory()
    with factory() as session:
        return run_expire_sweep(session)


@celery_app.task(name=CONSUME_OVERRIDE_GRANT_TASK)
def consume_override_grant(agent_run_id: str, action_fingerprint: str) -> bool:
    """Resume-path entrypoint: consume the single-use grant for one call."""
    factory: sessionmaker[Session] = create_session_factory()
    with factory() as session:
        return run_consume_grant(
            session,
            agent_run_id=uuid.UUID(agent_run_id),
            action_fingerprint=action_fingerprint,
        )
