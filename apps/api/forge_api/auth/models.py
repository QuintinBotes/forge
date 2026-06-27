"""Request/response DTOs for the /auth/* routes (Task 1.15 — auth & secrets)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from forge_api.auth.apikeys import APIKeyInfo
from forge_contracts.enums import APIKeyKind, UserRole


class APIKeyCreateRequest(BaseModel):
    """Body for minting a new Forge API key."""

    name: str = Field(min_length=1, max_length=255)
    role: UserRole = UserRole.MEMBER
    kind: APIKeyKind = APIKeyKind.SYSTEM
    expires_at: datetime | None = None


class APIKeyCreated(APIKeyInfo):
    """Mint response — carries the plaintext ``token`` exactly once."""

    token: str


class SecretCreateRequest(BaseModel):
    """Body for storing a BYOK secret in the encrypted vault."""

    name: str = Field(min_length=1, max_length=255)
    secret: str = Field(min_length=1)
    kind: APIKeyKind = APIKeyKind.MODEL_PROVIDER
    provider: str | None = None
    expires_at: datetime | None = None


class LoginRequest(BaseModel):
    """Body for beginning an OAuth sign-in flow."""

    provider: str = "github"
    redirect_uri: str | None = None


class OAuthChallenge(BaseModel):
    """The authorization-code descriptor returned to start an OAuth flow.

    No external call is made: the API returns the provider authorize URL and an
    anti-CSRF ``state`` for the client (Better Auth / frontend) to redirect to.
    """

    provider: str
    authorize_url: str
    state: str


__all__ = [
    "APIKeyCreateRequest",
    "APIKeyCreated",
    "LoginRequest",
    "OAuthChallenge",
    "SecretCreateRequest",
]
