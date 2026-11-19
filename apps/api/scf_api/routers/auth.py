"""Auth & secrets routes (Task 1.15 — auth & secrets).

Implements API-key authentication (OAuth + API key per spec), the encrypted BYOK
secret vault, and RBAC enforcement. ``/login`` and ``/logout`` are
unauthenticated (session bootstrap; ``/logout`` revokes the key the session
cookie carries), and so is ``GET /sign-in-link``, which redeems a one-time link
``scf-cli admin sign-in-link`` printed into that cookie; every other route
authenticates via ``Authorization: Bearer <api_key>`` (or ``X-API-Key``, or the
``forge_session`` cookie a browser sign-in set) and is gated by the caller's role
permissions. All responses are redacted — no plaintext secret or usable token is
ever returned except the one-time mint token.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Query, Response, status

from scf_api.auth import sign_in_link
from scf_api.auth.apikeys import APPROVALS_DECIDE, RUNS_EXECUTE, APIKeyInfo
from scf_api.auth.models import (
    APIKeyCreated,
    APIKeyCreateRequest,
    LoginRequest,
    OAuthCallbackRequest,
    OAuthChallenge,
    OAuthResult,
    PersonalTokenCreateRequest,
    SecretCreateRequest,
    SecretRotateRequest,
)
from scf_api.auth.oauth import (
    OAuthConfigError,
    OAuthExchangeError,
    OAuthStateError,
    UnsupportedOAuthProviderError,
)
from scf_api.auth.rbac import Permission, can
from scf_api.auth.service import (
    AuthedPrincipal,
    AuthenticationError,
    AuthServiceDep,
    is_own_origin,
    require_permission,
)
from scf_api.auth.vault import SecretExpiredError, SecretInfo, SecretNotFoundError
from scf_api.deps import SESSION_COOKIE, DbSession, Principal, SettingsDep
from scf_api.routers.saml import start_session
from scf_api.services.approval_service import owner_role
from scf_auth.rbac import has_at_least
from scf_authz import ROLE_RANK
from scf_contracts.enums import UserRole

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)

#: A sign-in link's URL is a secret: its answer is never cached or sent on as a referrer.
_SIGN_IN_LINK_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}

# Permission-gated principal dependencies (authenticate + authorize in one step).
KeyManager = Annotated[Principal, Depends(require_permission(Permission.MANAGE_KEYS))]
SecretManager = Annotated[Principal, Depends(require_permission(Permission.MANAGE_SECRETS))]


@router.post(
    "/login",
    response_model=OAuthChallenge,
    summary="Begin an OAuth sign-in flow (Google / GitHub / GitLab).",
)
def login(body: LoginRequest, service: AuthServiceDep) -> OAuthChallenge:
    return service.oauth_challenge(body.provider, body.redirect_uri)


@router.post(
    "/callback",
    response_model=OAuthResult,
    summary="Complete an OAuth flow: exchange the IdP code for tokens and a user.",
)
async def oauth_callback(body: OAuthCallbackRequest, service: AuthServiceDep) -> OAuthResult:
    try:
        return await service.exchange_oauth_code(
            body.provider,
            body.code,
            redirect_uri=body.redirect_uri,
            state=body.state,
            expected_state=body.expected_state,
        )
    except UnsupportedOAuthProviderError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except OAuthStateError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except OAuthConfigError as exc:
        # The provider is valid but this deployment has no client credentials.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    except OAuthExchangeError as exc:
        # The upstream IdP rejected the code or returned an unusable response.
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="End the session: revoke the session cookie's key and clear the cookie.",
)
def logout(
    service: AuthServiceDep,
    settings: SettingsDep,
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    origin: Annotated[str | None, Header()] = None,
    sec_fetch_site: Annotated[str | None, Header()] = None,
) -> Response:
    """Sign out a browser. Without a cookie there is nothing to end (204).

    Only the key the cookie carries is revoked, so a replay of it gets 401 and the
    person's other sessions and keys keep working. A sign-out started by another
    origin is refused, like every other state change the cookie authenticates.
    """
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    if not session_cookie:
        return response
    if not is_own_origin(origin, sec_fetch_site):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="a session cookie may change something only from SCF's own origin",
        )
    try:
        principal: Principal | None = service.authenticate(session_cookie)
    except AuthenticationError:
        principal = None  # expired or already revoked: only the cookie is left to clear
    if principal is not None and principal.credential_id is not None:
        service.api_keys.revoke(principal.workspace_id, principal.credential_id)
        service.audit.emit(
            action="auth.logout",
            principal=principal,
            target_type="platform_api_key",
            target_id=principal.credential_id,
        )
    response.delete_cookie(
        SESSION_COOKIE,
        httponly=True,
        secure=settings.public_url.startswith("https://"),
        samesite="lax",
    )
    return response


@router.get(
    "/sign-in-link",
    summary="Redeem a one-time sign-in link: set the session cookie and redirect.",
)
def redeem_sign_in_link(
    session: DbSession,
    settings: SettingsDep,
    token: Annotated[str | None, Query()] = None,
    next: Annotated[str | None, Query()] = None,
) -> Response:
    """A browser opens the link without credentials, so this route is public by design.

    A missing, unknown, expired or used link gets one answer, with no cookie.
    """
    user = sign_in_link.redeem(session, token) if token else None
    if user is None:
        logger.warning("sign-in link refused")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="this sign-in link is not valid",
            headers=_SIGN_IN_LINK_HEADERS,
        )
    logger.info("sign-in link redeemed workspace=%s user=%s", user.workspace_id, user.id)
    response = start_session(
        settings,
        workspace_id=user.workspace_id,
        user_id=user.id,
        role=UserRole.ADMIN,
        next_path=next,
        now=datetime.now(UTC),
        label="sign-in-link-session",
    )
    response.headers.update(_SIGN_IN_LINK_HEADERS)
    return response


@router.get(
    "/me",
    response_model=Principal,
    summary="Return the authenticated principal.",
)
def me(principal: AuthedPrincipal) -> Principal:
    return principal


@router.get(
    "/api-keys",
    response_model=list[APIKeyInfo],
    summary="List the workspace's API keys (redacted).",
)
def list_api_keys(principal: KeyManager, service: AuthServiceDep) -> list[APIKeyInfo]:
    return service.api_keys.list_keys(principal.workspace_id)


@router.post(
    "/api-keys",
    response_model=APIKeyCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Mint a new API key (BYOK / agent-runner). Token shown once.",
)
def create_api_key(
    body: APIKeyCreateRequest, principal: KeyManager, service: AuthServiceDep
) -> APIKeyCreated:
    # F37 AC8: no self-escalation — a key can never carry a role above its
    # creator's (the audit trail records the denied attempt).
    if not has_at_least(principal.role, body.role):
        service.audit.emit(
            action="apikey.created",
            principal=principal,
            target_type="platform_api_key",
            result="denied",
            details={"name": body.name, "requested_role": body.role.value},
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"escalation: requested role '{body.role.value}' exceeds "
                f"actor role '{principal.role.value}'"
            ),
        )
    info, token = service.api_keys.mint(
        workspace_id=principal.workspace_id,
        name=body.name,
        role=body.role,
        kind=body.kind,
        expires_at=body.expires_at,
    )
    service.audit.emit(
        action="apikey.created",
        principal=principal,
        target_type="platform_api_key",
        target_id=info.id,
        details={"name": info.name, "role": info.role.value, "kind": info.kind.value},
    )
    return APIKeyCreated(**info.model_dump(), token=token)


@router.post(
    "/personal-tokens",
    response_model=APIKeyCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Mint your own personal access token (a person only). Token shown once.",
)
def create_personal_token(
    body: PersonalTokenCreateRequest,
    principal: AuthedPrincipal,
    service: AuthServiceDep,
    session: DbSession,
) -> APIKeyCreated:
    """A token for another surface (tervin, the CLI), bound to the calling person.

    A session or a person-bound unscoped key (the web's credential) may mint one;
    a narrowed or unbound key may not, so a token never mints a token (spec L
    §6a). It reads as a viewer (plus ``approvals:decide`` when asked, deciding
    with its owner's current role) and expires. With ``start_runs`` it acts as an
    agent-runner instead, minted only while both the minting credential and its
    owner's current role grant ``run_agent`` (spec L §6b).
    """
    if not principal.decides or principal.narrowed:  # a session or a person-bound unscoped key
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="personal tokens are minted by a person: a session or your own unscoped key",
        )
    current = owner_role(session, principal)
    if current is None:  # deactivated, removed or expired: nothing to delegate
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="your account has no current role in this workspace",
        )
    if body.decide and ROLE_RANK[principal.role] < ROLE_RANK[current]:
        # The token decides with its owner's role: never more than the minting key's.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="a deciding token is minted by a credential with your full role",
        )
    if body.start_runs and not (
        can(principal.role, Permission.RUN_AGENT) and can(current, Permission.RUN_AGENT)
    ):
        # The token starts runs as an agent-runner: never more than its minter or owner may.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="a token that starts runs needs run_agent in your current role",
        )
    scopes = ["read"]
    if body.decide:
        scopes.append(APPROVALS_DECIDE)
    if body.start_runs:
        scopes.append(RUNS_EXECUTE)
    info, token = service.api_keys.mint(
        workspace_id=principal.workspace_id,
        name=body.name,
        role=UserRole.VIEWER,
        user_id=principal.user_id,
        expires_at=datetime.now(UTC) + timedelta(days=body.expires_in_days),
        scopes=scopes,
        surface=body.surface,
    )
    service.audit.emit(
        action="apikey.created",
        principal=principal,
        target_type="platform_api_key",
        target_id=info.id,
        details={
            "name": info.name,
            "role": info.role.value,
            "personal": True,
            "surface": info.surface,
            "scopes": info.scopes,
            "expires_at": info.expires_at.isoformat() if info.expires_at else None,
        },
    )
    return APIKeyCreated(**info.model_dump(), token=token)


@router.delete(
    "/api-keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an API key.",
)
def revoke_api_key(key_id: uuid.UUID, principal: KeyManager, service: AuthServiceDep) -> Response:
    if not service.api_keys.revoke(principal.workspace_id, key_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    service.audit.emit(
        action="apikey.revoked",
        principal=principal,
        target_type="platform_api_key",
        target_id=key_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/secrets",
    response_model=list[SecretInfo],
    summary="List the workspace's BYOK secrets (redacted, no plaintext).",
)
def list_secrets(principal: SecretManager, service: AuthServiceDep) -> list[SecretInfo]:
    return service.vault.list_secrets(principal.workspace_id)


@router.post(
    "/secrets",
    response_model=SecretInfo,
    status_code=status.HTTP_201_CREATED,
    summary="Store a BYOK secret (encrypted at rest). Returns redacted metadata.",
)
def create_secret(
    body: SecretCreateRequest, principal: SecretManager, service: AuthServiceDep
) -> SecretInfo:
    info = service.vault.put_secret(
        workspace_id=principal.workspace_id,
        name=body.name,
        secret=body.secret,
        kind=body.kind,
        provider=body.provider,
        expires_at=body.expires_at,
    )
    # Register the stored value with the canonical redactor so it is scrubbed
    # from any future log/audit text, then audit the mutation (redacted).
    service.audit.redactor.register_known_secret(body.secret)
    service.audit.emit(
        action="secret.created",
        principal=principal,
        target_type="api_key",
        target_id=info.id,
        details={"name": info.name, "kind": info.kind.value, "provider": info.provider},
    )
    return info


@router.post(
    "/secrets/{secret_id}/check",
    summary="Verify a BYOK secret decrypts and is not expired (no plaintext returned).",
)
def check_secret(
    secret_id: uuid.UUID, principal: SecretManager, service: AuthServiceDep
) -> dict[str, bool]:
    """Resolve a stored credential server-side to confirm it is usable.

    Returns ``{"ok": true}`` when the secret decrypts and is unexpired; an expired
    credential yields HTTP 409 ("rotate this credential"), a missing one 404. The
    plaintext is never returned — only the usability verdict (HARD-13 AC8).
    """
    try:
        service.vault.get_secret(principal.workspace_id, secret_id)
    except SecretExpiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="secret has expired; rotate this credential",
        ) from exc
    except SecretNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="secret not found"
        ) from exc
    return {"ok": True}


@router.post(
    "/secrets/{secret_id}/rotate",
    response_model=SecretInfo,
    summary="Rotate a BYOK secret's value (admin-only). Returns redacted metadata.",
)
def rotate_secret(
    secret_id: uuid.UUID,
    body: SecretRotateRequest,
    principal: SecretManager,
    service: AuthServiceDep,
) -> SecretInfo:
    try:
        info = service.vault.rotate_secret(
            workspace_id=principal.workspace_id,
            secret_id=secret_id,
            new_secret=body.secret,
            expires_at=body.expires_at,
        )
    except SecretExpiredError as exc:
        # An expired credential can still be rotated; this maps the read-time
        # expiry error uniformly should a resolve happen inside rotation.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="secret has expired; rotate this credential",
        ) from exc
    except SecretNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="secret not found"
        ) from exc
    # Register the new value with the canonical redactor and audit the rotation.
    service.audit.redactor.register_known_secret(body.secret)
    service.audit.emit(
        action="secret.rotated",
        principal=principal,
        target_type="api_key",
        target_id=info.id,
        details={"name": info.name, "kind": info.kind.value, "provider": info.provider},
    )
    return info


@router.delete(
    "/secrets/{secret_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a BYOK secret.",
)
def delete_secret(
    secret_id: uuid.UUID, principal: SecretManager, service: AuthServiceDep
) -> Response:
    try:
        service.vault.delete_secret(principal.workspace_id, secret_id)
    except SecretNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="secret not found"
        ) from exc
    service.audit.emit(
        action="secret.deleted",
        principal=principal,
        target_type="api_key",
        target_id=secret_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
