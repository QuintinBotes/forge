"""Auth & secrets router stubs (filled by Task 1.15 — auth & secrets).

Login / session and API-key management are unauthenticated or
self-authenticating, so this router does *not* carry the global principal
dependency; ``/auth/me`` echoes the resolved principal once real auth lands.
"""

from __future__ import annotations

from fastapi import APIRouter

from forge_api._stubs import NotImplementedResponse, eventual, not_implemented
from forge_api.deps import CurrentPrincipal, Principal

router = APIRouter(
    prefix="/auth",
    tags=["auth"],
    responses={501: {"model": NotImplementedResponse}},
)

_R = "auth"


@router.post(
    "/login",
    response_model=NotImplementedResponse,
    status_code=501,
    summary="Begin an OAuth / credential login flow.",
)
def login() -> NotImplementedResponse:
    return not_implemented(_R, "login")


@router.post(
    "/logout",
    response_model=NotImplementedResponse,
    status_code=501,
    summary="End the current session.",
)
def logout() -> NotImplementedResponse:
    return not_implemented(_R, "logout")


@router.get(
    "/me",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(Principal, "Return the authenticated principal."),
)
def me(principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "me")


@router.get(
    "/api-keys",
    response_model=NotImplementedResponse,
    status_code=501,
    summary="List the workspace's API keys (redacted).",
)
def list_api_keys(principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "list_api_keys")


@router.post(
    "/api-keys",
    response_model=NotImplementedResponse,
    status_code=501,
    summary="Mint a new API key (BYOK / agent-runner).",
)
def create_api_key(principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "create_api_key")
