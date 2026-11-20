"""A token that belongs to exactly one agent run (#256, spec 011 FR-013).

A run process in its own container or Job reaches its run record through SCF's API with
this token and never holds the database credential. It is not an API key and holds no
role: ``AuthService.authenticate`` refuses it, so no route guarded by a role or a scope
accepts it, and :func:`require_run_token`, used only by the run-record router, accepts it
for its own run alone.

The token names its run and workspace. Only its SHA-256 and expiry are kept, on the run's
own row under ``agent_run.output["run_token"]``, so the binding needs no migration.
Issuing again replaces the previous token; :func:`revoke_run_token` removes it; recording
the run's outcome at close-out replaces ``output`` and so removes it too.

The run process is SCF's own code, trusted like the worker (spec 011, Assumptions): the
token limits what a leaked credential can do; it is not a sandbox boundary.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from scf_api.deps import DbSession
from scf_api.services.audit import SqlAuditWriter
from scf_api.settings import get_settings
from scf_contracts.audit import AuditEvent
from scf_db.models import AgentRun

__all__ = [
    "RUN_TOKEN_KEY",
    "RUN_TOKEN_PREFIX",
    "IssuedRunToken",
    "RunCaller",
    "audit_run",
    "issue_run_token",
    "require_run_token",
    "revoke_run_token",
]

#: Every run token starts with this; no API key, session JWT or personal token does.
RUN_TOKEN_PREFIX = "scf_run_"
#: The ``agent_run.output`` key the live token's digest and expiry are kept under.
RUN_TOKEN_KEY = "run_token"


@dataclass(frozen=True)
class IssuedRunToken:
    """A freshly issued token; ``token`` is the plaintext, returned once and never stored."""

    run_id: uuid.UUID
    workspace_id: uuid.UUID
    expires_at: datetime
    token: str = field(repr=False)


@dataclass(frozen=True)
class RunCaller:
    """The run a verified token belongs to; both ids come from the token, never the request."""

    run_id: uuid.UUID
    workspace_id: uuid.UUID
    expires_at: datetime


def _digest(token: str) -> str:
    # The token carries 256 random bits, so an unkeyed hash cannot be reversed.
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def audit_run(
    session: Session,
    action: str,
    caller: RunCaller,
    *,
    actor_type: str,
) -> None:
    """One audit entry on ``session`` with the workspace, the run and the token's expiry."""
    SqlAuditWriter(session).emit(
        AuditEvent(
            workspace_id=caller.workspace_id,
            action=action,
            actor_type=actor_type,
            actor_label=f"{actor_type}:run_token",
            target_type="agent_run",
            target_id=caller.run_id,
            details={"run_id": str(caller.run_id), "expires_at": caller.expires_at.isoformat()},
        )
    )


def issue_run_token(
    session: Session,
    *,
    run_id: uuid.UUID,
    workspace_id: uuid.UUID,
    run_deadline_seconds: float,
    now: datetime | None = None,
) -> IssuedRunToken:
    """Issue the token of one open run of one workspace, replacing any earlier one.

    ``run_deadline_seconds`` is the run's wall-clock limit plus the reconciler's grace
    (``scf_worker.run_executor.run_deadline_seconds``); the token lives that long, or
    ``FORGE_RUN_TOKEN_TTL_SECONDS`` if shorter. ``LookupError`` when the run is not this
    workspace's or is closed out; ``ValueError`` for a deadline that is not a finite,
    positive number of seconds.
    """
    if not 0 < run_deadline_seconds < math.inf:
        raise ValueError("run_deadline_seconds must be a finite, positive number of seconds")
    cap = get_settings().run_token_ttl_seconds
    lifetime = min(run_deadline_seconds, cap) if cap else run_deadline_seconds
    expires_at = (now or datetime.now(UTC)) + timedelta(seconds=lifetime)
    row = session.get(AgentRun, run_id, with_for_update=True)
    if row is None or row.workspace_id != workspace_id or row.completed_at is not None:
        raise LookupError(f"no open run {run_id} in workspace {workspace_id}")
    token = f"{RUN_TOKEN_PREFIX}{run_id.hex}_{workspace_id.hex}_{secrets.token_urlsafe(32)}"
    record = {"sha256": _digest(token), "expires_at": expires_at.isoformat()}
    row.output = {**(row.output or {}), RUN_TOKEN_KEY: record}
    issued = IssuedRunToken(run_id, workspace_id, expires_at, token)
    audit_run(
        session,
        "run_token.issued",
        RunCaller(run_id, workspace_id, expires_at),
        actor_type="system",
    )
    session.commit()
    return issued


def revoke_run_token(session: Session, *, run_id: uuid.UUID, workspace_id: uuid.UUID) -> bool:
    """Refuse the run's token from now on (for the worker to call); ``False`` if it had none."""
    row = session.get(AgentRun, run_id, with_for_update=True)
    output = dict(row.output or {}) if row is not None else {}
    if row is None or row.workspace_id != workspace_id or output.pop(RUN_TOKEN_KEY, None) is None:
        return False
    row.output = output
    session.commit()
    return True


def _claims(token: str) -> tuple[uuid.UUID, uuid.UUID] | None:
    """The run and workspace a token names; ``None`` for anything that is not a run token."""
    if not token.startswith(RUN_TOKEN_PREFIX):
        return None
    run_hex, _, rest = token.removeprefix(RUN_TOKEN_PREFIX).partition("_")
    workspace_hex, _, secret = rest.partition("_")
    if len(run_hex) != 32 or len(workspace_hex) != 32 or not secret:
        return None
    try:
        return uuid.UUID(hex=run_hex), uuid.UUID(hex=workspace_hex)
    except ValueError:
        return None


def require_run_token(
    run_id: uuid.UUID,
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> RunCaller:
    """Accept only the live token of the run in the path, from ``Authorization: Bearer``.

    Every refusal is the same ``401`` and names nothing (ADR-0007): an absent, malformed,
    expired, revoked or closed-out token, any other credential, and another run's token,
    which is refused before the path's run is looked up, so the answer never says whether
    that run exists. No other header or cookie is read: there is no fallback.
    """
    refused = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="this route accepts only its own run's live run token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    scheme, _, token = (authorization or "").partition(" ")
    claims = _claims(token.strip()) if scheme.lower() == "bearer" else None
    if claims is None or claims[0] != run_id:
        raise refused
    row = session.execute(
        select(AgentRun.output, AgentRun.completed_at).where(
            AgentRun.id == claims[0], AgentRun.workspace_id == claims[1]
        )
    ).first()
    record = (row.output or {}).get(RUN_TOKEN_KEY) if row is not None else None
    if not isinstance(record, dict) or not hmac.compare_digest(
        str(record.get("sha256", "")), _digest(token.strip())
    ):
        raise refused
    expires_at = datetime.fromisoformat(record["expires_at"])
    if row is None or row.completed_at is not None or expires_at <= datetime.now(UTC):
        raise refused
    return RunCaller(claims[0], claims[1], expires_at)
