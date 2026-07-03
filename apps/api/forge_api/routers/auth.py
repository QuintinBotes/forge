"""Auth & secrets routes (Task 1.15 — auth & secrets).

Implements API-key authentication (OAuth + API key per spec), the encrypted BYOK
secret vault, and RBAC enforcement. ``/login`` and ``/logout`` are
unauthenticated (session bootstrap); every other route authenticates via
``Authorization: Bearer <api_key>`` (or ``X-API-Key``) and is gated by the
caller's role permissions. All responses are redacted — no plaintext secret or
usable token is ever returned except the one-time mint token.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status

from forge_api.auth.apikeys import APIKeyInfo
from forge_api.auth.models import (
    APIKeyCreated,
    APIKeyCreateRequest,
    LoginRequest,
    OAuthCallbackRequest,
    OAuthChallenge,
    OAuthResult,
    SecretCreateRequest,
)
from forge_api.auth.oauth import (
    OAuthConfigError,
    OAuthExchangeError,
    OAuthStateError,
    UnsupportedOAuthProviderError,
)
from forge_api.auth.rbac import Permission
from forge_api.auth.service import (
    AuthedPrincipal,
    AuthServiceDep,
    require_permission,
)
from forge_api.auth.vault import SecretInfo, SecretNotFoundError
from forge_api.deps import Principal
from forge_auth.rbac import has_at_least

router = APIRouter(prefix="/auth", tags=["auth"])

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
async def oauth_callback(
    body: OAuthCallbackRequest, service: AuthServiceDep
) -> OAuthResult:
    try:
        return await service.exchange_oauth_code(
            body.provider,
            body.code,
            redirect_uri=body.redirect_uri,
            state=body.state,
            expected_state=body.expected_state,
        )
    except UnsupportedOAuthProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except OAuthStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except OAuthConfigError as exc:
        # The provider is valid but this deployment has no client credentials.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    except OAuthExchangeError as exc:
        # The upstream IdP rejected the code or returned an unusable response.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="End the current session (stateless: client discards its token).",
)
def logout() -> Response:
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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


@router.delete(
    "/api-keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an API key.",
)
def revoke_api_key(
    key_id: uuid.UUID, principal: KeyManager, service: AuthServiceDep
) -> Response:
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
