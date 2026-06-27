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
    OAuthChallenge,
    SecretCreateRequest,
)
from forge_api.auth.rbac import Permission
from forge_api.auth.service import (
    AuthedPrincipal,
    AuthServiceDep,
    require_permission,
)
from forge_api.auth.vault import SecretInfo, SecretNotFoundError
from forge_api.deps import Principal

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
    info, token = service.api_keys.mint(
        workspace_id=principal.workspace_id,
        name=body.name,
        role=body.role,
        kind=body.kind,
        expires_at=body.expires_at,
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
    return service.vault.put_secret(
        workspace_id=principal.workspace_id,
        name=body.name,
        secret=body.secret,
        kind=body.kind,
        provider=body.provider,
        expires_at=body.expires_at,
    )


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
    return Response(status_code=status.HTTP_204_NO_CONTENT)
