"""OpenID Connect protocol surface (F33): SP-initiated login + callback.

The OIDC sibling of ``routers/saml.py``. ``GET /auth/oidc/{slug}/login`` mints a
``state`` / ``nonce`` / PKCE verifier, persists them in the one-shot transaction
store, and 302s to the IdP authorize endpoint. ``GET /auth/oidc/{slug}/callback``
consumes the transaction, exchanges the code, **validates** the returned ID
token (signature/iss/aud/exp/iat/nonce), JIT-provisions the user via the shared
``sso/provisioning.py`` path (identical to the SAML ACS), mints the Forge
session cookie, and redirects to the RelayState target.

Intentionally unauthenticated — the validated ID token *is* the authentication.
The workspace is resolved from the URL slug, never from token fields
(confused-deputy guard), exactly as the SAML surface does.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from forge_api.auth.service import get_auth_service
from forge_api.deps import DbSession, SettingsDep
from forge_api.routers.saml import SESSION_TTL, _safe_next
from forge_api.settings import Settings
from forge_api.sso.config_service import SsoConfigService
from forge_api.sso.errors import OidcValidationError, SsoConfigError
from forge_api.sso.oidc import (
    InMemoryOidcStateStore,
    OidcClient,
    OidcTransaction,
    build_authorize_url,
    extract_identity,
    generate_pkce,
    new_nonce,
    new_state,
)
from forge_api.sso.provisioning import emit_sso_audit, link_or_jit_provision
from forge_contracts.enums import APIKeyKind, UserRole
from forge_db.models import OidcConfiguration, Workspace
from forge_db.models.enums import ExternalIdentityProvider

router = APIRouter(prefix="/auth/oidc", tags=["oidc"])

# Process-wide login-transaction store (single-process default; a Redis/DB store
# for multi-process deployments is parked — see forge_api.sso.oidc).
_state_store = InMemoryOidcStateStore()


def get_oidc_state_store() -> InMemoryOidcStateStore:
    """Dependency seam so tests inject a fresh store shared across login+callback."""
    return _state_store


def get_oidc_client() -> OidcClient:
    """Dependency seam so tests inject a client bound to a mock IdP transport."""
    return OidcClient()


StateStoreDep = Annotated[InMemoryOidcStateStore, Depends(get_oidc_state_store)]
OidcClientDep = Annotated[OidcClient, Depends(get_oidc_client)]


def _load(
    session: Session, settings: Settings, slug: str
) -> tuple[Workspace, OidcConfiguration, SsoConfigService]:
    service = SsoConfigService(session, public_url=settings.public_url)
    try:
        pair = service.get_oidc_config_by_slug(slug)
    except OperationalError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SSO realm unavailable (identity store unreachable)",
        ) from exc
    if pair is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown SSO realm")
    workspace, config = pair
    return workspace, config, service


def _require_enabled(config: OidcConfiguration) -> None:
    if not config.enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="SSO is disabled for this workspace"
        )


@router.get("/{slug}/login", summary="SP-initiated login (302 to the IdP)")
def oidc_login(
    slug: str,
    session: DbSession,
    settings: SettingsDep,
    client: OidcClientDep,
    store: StateStoreDep,
    next: Annotated[str | None, Query()] = None,
) -> RedirectResponse:
    _workspace, config, service = _load(session, settings, slug)
    _require_enabled(config)
    dto = service.oidc_config_dto(config)
    try:
        discovery = client.discover(dto)
    except SsoConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "oidc_discovery_failed", "reason": str(exc)},
        ) from exc
    state, nonce = new_state(), new_nonce()
    verifier, challenge = generate_pkce()
    store.put(
        OidcTransaction(
            state=state,
            nonce=nonce,
            code_verifier=verifier,
            relay_state=_safe_next(next),
        ),
        settings.oidc_transaction_ttl_seconds,
    )
    authorize_url = build_authorize_url(
        discovery,
        client_id=dto.client_id,
        redirect_uri=service.oidc_urls(slug)["redirect_uri"],
        scopes=list(dto.scopes),
        state=state,
        nonce=nonce,
        code_challenge=challenge,
    )
    return RedirectResponse(url=authorize_url, status_code=status.HTTP_302_FOUND)


@router.get("/{slug}/callback", summary="OIDC authorization-code callback")
def oidc_callback(
    slug: str,
    session: DbSession,
    settings: SettingsDep,
    client: OidcClientDep,
    store: StateStoreDep,
    state: Annotated[str | None, Query()] = None,
    code: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
) -> RedirectResponse:
    workspace, config, service = _load(session, settings, slug)
    _require_enabled(config)
    now = datetime.now(UTC)

    def _login_failed(reason: str, http_status: int = status.HTTP_400_BAD_REQUEST) -> HTTPException:
        emit_sso_audit(
            session,
            workspace_id=workspace.id,
            action="sso.login_failed",
            actor_type="idp",
            result="failure",
            details={"reason": reason, "protocol": "oidc"},
        )
        session.commit()
        return HTTPException(
            status_code=http_status,
            detail={"error": "oidc_login_failed", "reason": reason},
        )

    if error:
        raise _login_failed(f"idp_error:{error}")
    if not state:
        raise _login_failed("missing_state")
    txn = store.consume(state)
    if txn is None:
        raise _login_failed("invalid_state")
    if not code:
        raise _login_failed("missing_code")

    dto = service.oidc_config_dto(config)
    try:
        discovery = client.discover(dto)
        tokens = client.exchange_code(
            discovery,
            code=code,
            redirect_uri=service.oidc_urls(slug)["redirect_uri"],
            client_id=dto.client_id,
            client_secret=service.decrypt_oidc_secret(config),
            code_verifier=txn.code_verifier,
        )
        claims = client.validate_id_token(
            str(tokens["id_token"]),
            discovery=discovery,
            client_id=dto.client_id,
            nonce=txn.nonce,
            clock_skew_seconds=settings.oidc_clock_skew_seconds,
            now=now,
        )
        identity = extract_identity(claims, dto)
    except OidcValidationError as exc:
        raise _login_failed(exc.reason) from exc
    except SsoConfigError as exc:
        raise _login_failed("oidc_config_error", status.HTTP_502_BAD_GATEWAY) from exc

    try:
        user = link_or_jit_provision(
            session=session,
            config=config,
            identity=identity,
            provider=ExternalIdentityProvider.OIDC,
        )
    except SsoConfigError as exc:
        raise _login_failed("provisioning_rejected", status.HTTP_403_FORBIDDEN) from exc

    emit_sso_audit(
        session,
        workspace_id=workspace.id,
        action="sso.login",
        actor_id=user.id,
        actor_type="user",
        target_type="user",
        target_id=user.id,
        details={"protocol": "oidc"},
    )
    session.commit()

    _info, token = get_auth_service().bootstrap_key(
        workspace_id=workspace.id,
        name=f"sso-session-{uuid.uuid4().hex[:8]}",
        role=UserRole(user.role.value),
        user_id=user.id,
        kind=APIKeyKind.SYSTEM,
        expires_at=now + SESSION_TTL,
    )
    response = RedirectResponse(url=_safe_next(txn.relay_state), status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        "forge_session",
        token,
        httponly=True,
        secure=settings.public_url.startswith("https://"),
        samesite="lax",
        max_age=int(SESSION_TTL.total_seconds()),
    )
    return response
