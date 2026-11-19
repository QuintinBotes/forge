"""Device sign-in for a client with no browser and no stored secret (#207, RFC 8628).

``scf-cli login`` asks ``POST /auth/device/code`` for a code, with no credential. A
person signed in to SCF approves or denies its short user code at
``POST /auth/device/approve``, which binds the request to their workspace and
identity. The client polls ``POST /auth/device/token`` with its device code and
receives a run-start personal token of #206 once. Like a sign-in link (#260), only
SHA-256 hashes of the codes are stored, a request works once, and it expires. The
workspace is unknown until someone approves, so the table is not workspace-scoped and
the request is audited in that workspace when it is approved or denied.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, Update, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from scf_api.auth.apikeys import RUNS_EXECUTE, APIKeyInfo, APIKeyStore
from scf_api.auth.rbac import Permission, can
from scf_api.deps import Principal
from scf_api.services.approval_service import owner_role
from scf_api.services.auth_audit import actor_type_for
from scf_api.sso.provisioning import emit_sso_audit
from scf_contracts.enums import UserRole
from scf_db.models import DeviceAuthorization

#: How long a request can be approved and its token collected.
LIFETIME = timedelta(minutes=10)
#: The least time between two polls of one request (RFC 8628 ``interval``).
INTERVAL = timedelta(seconds=5)
#: Wrong user codes naming one request (by their first half) that end it.
MAX_WRONG_CODES = 5
#: Pending requests one address may hold: a flood is refused, not stored.
MAX_PENDING_PER_ADDRESS = 20
#: How long the issued token works.
TOKEN_LIFETIME = timedelta(days=1)
#: The run-start scope set of #206, the only one this flow issues.
SCOPES = ("read", RUNS_EXECUTE)
#: Where a person approves or denies a user code, under ``public_url``.
APPROVE_PATH = "/auth/device/approve"
#: RFC 8628 §6.1: 20 consonants, so a code spells no word and has no look-alikes.
ALPHABET = "BCDFGHJKLMNPQRSTVWXZ"
_HALF = 4

PENDING, APPROVED, DENIED, ISSUED, INVALID = "pending", "approved", "denied", "issued", "invalid"


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _rowcount(result: Any) -> int:
    return cast("CursorResult[Any]", result).rowcount


def _update() -> Update:
    # "fetch": SQLite returns naive datetimes, which in-Python evaluation cannot compare.
    return update(DeviceAuthorization).execution_options(synchronize_session="fetch")


def start(
    session: Session,
    *,
    client_name: str,
    address: str,
    agent: str | None,
    now: datetime | None = None,
) -> tuple[DeviceAuthorization, str, str] | None:
    """Store a pending request; return it, its device code and its user code (``XXXX-XXXX``).

    ``None`` when ``address`` already holds :data:`MAX_PENDING_PER_ADDRESS` pending
    requests. A user code a pending request holds is drawn again.
    """
    now = now or datetime.now(UTC)
    # Anyone may start a request, so requests that ended a day ago are dropped here.
    session.execute(
        delete(DeviceAuthorization).where(DeviceAuthorization.expires_at < now - timedelta(days=1))
    )
    session.commit()
    pending = session.scalar(
        select(func.count()).where(
            DeviceAuthorization.requester_address == address,
            DeviceAuthorization.status == PENDING,
            DeviceAuthorization.expires_at > now,
        )
    )
    if (pending or 0) >= MAX_PENDING_PER_ADDRESS:
        return None
    for _ in range(5):
        device_code = secrets.token_urlsafe(32)
        user_code = "".join(secrets.choice(ALPHABET) for _ in range(2 * _HALF))
        request = DeviceAuthorization(
            device_code_hash=_hash(device_code),
            user_code_selector=user_code[:_HALF],
            user_code_hash=_hash(user_code),
            client_name=client_name,
            status=PENDING,
            expires_at=now + LIFETIME,
            requester_address=address[:64],
            requester_agent=agent[:255] if agent else None,
        )
        session.add(request)
        try:
            session.commit()
        except IntegrityError:  # a pending request holds this user code
            session.rollback()
            continue
        return request, device_code, f"{user_code[:_HALF]}-{user_code[_HALF:]}"
    raise RuntimeError("no free user code after 5 draws")


def find(
    session: Session, user_code: str, *, now: datetime | None = None
) -> DeviceAuthorization | None:
    """The pending request ``user_code`` names, else ``None``.

    The first half selects the request and the whole code is compared with its hash in
    constant time. A wrong code counts against the request its first half selects,
    which ends after :data:`MAX_WRONG_CODES`. An unknown code changes nothing, and a
    code more than one pending request holds names none of them.
    """
    now = now or datetime.now(UTC)
    code = "".join(c for c in user_code.upper() if c in ALPHABET)
    live = (
        DeviceAuthorization.user_code_selector == code[:_HALF],
        DeviceAuthorization.status == PENDING,
        DeviceAuthorization.expires_at > now,
    )
    requests = session.scalars(select(DeviceAuthorization).where(*live)).all()
    digest = _hash(code)
    matches = [r for r in requests if hmac.compare_digest(r.user_code_hash, digest)]
    if matches or not requests:
        return matches[0] if len(matches) == 1 else None
    session.execute(
        _update().where(*live).values(failed_attempts=DeviceAuthorization.failed_attempts + 1)
    )
    session.execute(
        _update()
        .where(*live, DeviceAuthorization.failed_attempts >= MAX_WRONG_CODES)
        .values(status=INVALID)
    )
    session.commit()
    return None


def _audit(
    session: Session,
    request: DeviceAuthorization,
    action: str,
    *,
    actor_type: str,
    target_type: str = "device_authorization",
    target_id: uuid.UUID | None = None,
    result: str = "success",
    **details: Any,
) -> None:
    assert request.workspace_id is not None  # bound by decide() before any audit
    emit_sso_audit(
        session,
        workspace_id=request.workspace_id,
        action=action,
        actor_id=request.user_id,
        actor_type=actor_type,
        target_type=target_type,
        target_id=target_id or request.id,
        result=result,
        details={
            "client_name": request.client_name,
            "requester_address": request.requester_address,
            "requester_agent": request.requester_agent,
            **details,
        },
    )


def decide(
    session: Session,
    request: DeviceAuthorization,
    principal: Principal,
    *,
    approve: bool,
    now: datetime | None = None,
) -> bool:
    """Bind a pending request to ``principal``, approved or denied, and audit it.

    Returns ``False`` when the request is no longer pending (decided or expired since).
    """
    now = now or datetime.now(UTC)
    bound = session.execute(
        _update()
        .where(
            DeviceAuthorization.id == request.id,
            DeviceAuthorization.status == PENDING,
            DeviceAuthorization.expires_at > now,
        )
        .values(
            status=APPROVED if approve else DENIED,
            workspace_id=principal.workspace_id,
            user_id=principal.user_id,
        )
    )
    if _rowcount(bound) != 1:
        session.rollback()
        return False
    actor_type = actor_type_for(principal)
    _audit(
        session,
        request,
        "auth.device_code_requested",
        actor_type=actor_type,
        requested_at=_aware(request.created_at).isoformat(),
    )
    action = "auth.device_code_approved" if approve else "auth.device_code_denied"
    _audit(session, request, action, actor_type=actor_type)
    session.commit()
    return True


def collect(
    session: Session, device_code: str, api_keys: APIKeyStore, *, now: datetime | None = None
) -> tuple[APIKeyInfo, str] | str:
    """The token for the approved request ``device_code`` names, once; else an RFC 8628 error.

    The token is minted only here, so no plaintext token is ever stored: one
    conditional UPDATE consumes the request and commits before the mint, so of
    concurrent or retried polls at most one gets a token.
    """
    now = now or datetime.now(UTC)
    request = session.scalar(
        select(DeviceAuthorization).where(
            DeviceAuthorization.device_code_hash == _hash(device_code)
        )
    )
    if request is None or request.status == ISSUED:
        return "invalid_grant"
    if request.status == INVALID or _aware(request.expires_at) <= now:
        return "expired_token"
    last, request.last_polled_at = request.last_polled_at, now
    session.commit()
    if last is not None and now - _aware(last) < INTERVAL:
        return "slow_down"
    if request.status == PENDING:
        return "authorization_pending"
    if request.status == DENIED:
        return "access_denied"
    assert request.workspace_id is not None and request.user_id is not None  # set on approval
    owner = Principal(user_id=request.user_id, workspace_id=request.workspace_id)
    current = owner_role(session, owner)
    # Demoted, deactivated or removed since approving: the token would not work.
    allowed = current is not None and can(current, Permission.RUN_AGENT)
    taken = session.execute(
        update(DeviceAuthorization)
        .where(DeviceAuthorization.id == request.id, DeviceAuthorization.status == APPROVED)
        .values(status=ISSUED if allowed else DENIED)
    )
    if _rowcount(taken) != 1:
        session.rollback()
        return "invalid_grant"
    if not allowed:
        _audit(
            session,
            request,
            "auth.device_token_issued",
            actor_type="user",
            result="denied",
            reason="owner_may_not_run",
        )
        session.commit()
        return "access_denied"
    # Taken before minting: a failure from here costs the client an invalid_grant, never a
    # second key (the database key store commits a key on its own).
    session.commit()
    info, token = api_keys.mint(
        workspace_id=request.workspace_id,
        name=request.client_name,
        role=UserRole.VIEWER,
        user_id=request.user_id,
        expires_at=now + TOKEN_LIFETIME,
        scopes=list(SCOPES),
        surface="cli",
    )
    _audit(
        session,
        request,
        "auth.device_token_issued",
        actor_type="user",
        target_type="platform_api_key",
        target_id=info.id,
        scopes=info.scopes,
        expires_at=info.expires_at.isoformat() if info.expires_at else None,
    )
    session.commit()
    return info, token
