"""Frozen auth & secrets contract (cross-cutting/F37-auth-secrets-byok).

DTOs + Protocols for the auth/secrets spine: the authenticated
:class:`Principal`, the web↔API :class:`SessionClaims` JWT claim set, BYOK
secret metadata, platform API-key DTOs, and the ``Vault`` / ``KeyProvider`` /
``RateLimiter`` / ``SecretRedactor`` seams implemented by ``forge_auth``
(``packages/auth-sdk``).

Foundation notes (conform-to-foundation):

* :class:`~forge_contracts.enums.UserRole` already exists in
  ``forge_contracts.enums`` and is reused verbatim (not mirrored).
* :class:`~forge_contracts.authz.PrincipalType` was introduced by F30
  (``forge_contracts.authz``) before this module landed; it is re-exported here
  so consumers can import it from the F37-canonical location.
* Audit contracts (``AuditEvent`` / ``AuditSink``) are owned by
  ``cross-cutting/F39-audit-log`` in :mod:`forge_contracts.audit` — F37 only
  emits them.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from forge_contracts.authz import PrincipalType
from forge_contracts.enums import UserRole

__all__ = [
    "CreatedKey",
    "KeyProvider",
    "OAuthProvider",
    "PlatformKeyKind",
    "PlatformKeyMeta",
    "Principal",
    "PrincipalType",
    "RateDecision",
    "RateLimiter",
    "SecretMeta",
    "SecretRedactor",
    "SessionClaims",
    "UserRole",
    "Vault",
]


class OAuthProvider(StrEnum):
    """OAuth sign-in providers (spec Integrations → V1)."""

    GOOGLE = "google"
    GITHUB = "github"
    GITLAB = "gitlab"


class PlatformKeyKind(StrEnum):
    """Inbound platform API-key kinds (controls token prefix + lifecycle).

    Distinct from :class:`~forge_contracts.enums.APIKeyKind` (BYOK secrets Forge
    decrypts to use *outbound*); platform keys are one-way hashed and only ever
    verified.
    """

    PERSONAL = "personal"
    SERVICE = "service"
    AGENT_RUNNER = "agent_runner"


class Principal(BaseModel):
    """The authenticated identity attached to every request."""

    model_config = ConfigDict(frozen=True)

    type: PrincipalType
    id: UUID  # user id | platform_api_key id | service id
    workspace_id: UUID
    role: UserRole  # flat role (V1); F30 layers scoped grants on top
    display: str  # e.g. "user:alice@x.com" / "key:CI deploy bot"
    email: str | None = None


class SessionClaims(BaseModel):
    """The JWT claim set minted by the web auth layer and verified by the API."""

    model_config = ConfigDict(frozen=True)

    sub: UUID  # forge user id
    wsid: UUID  # workspace id
    role: UserRole
    email: str | None = None
    aud: str = "forge-api"
    iss: str = "forge-web"
    exp: int  # unix seconds
    iat: int


class SecretMeta(BaseModel):
    """BYOK secret metadata — NEVER carries the secret value."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    kind: str  # APIKeyKind value
    provider: str | None = None
    key_prefix: str | None = None
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    created_at: datetime


class CreatedKey(BaseModel):
    """Returned EXACTLY once on platform-key creation (carries the token)."""

    id: UUID
    name: str
    kind: PlatformKeyKind
    role: UserRole
    token: str  # plaintext — present only in the create response
    key_prefix: str
    expires_at: datetime | None = None


class PlatformKeyMeta(BaseModel):
    """Platform API-key list/detail view — no token, no hash."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    kind: PlatformKeyKind
    role: UserRole
    key_prefix: str
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime


class RateDecision(BaseModel):
    """The outcome of a rate-limit check."""

    model_config = ConfigDict(frozen=True)

    allowed: bool
    remaining: int
    retry_after_seconds: float = 0.0


@runtime_checkable
class Vault(Protocol):
    """Envelope-encryption seam for BYOK secrets (per-workspace isolation)."""

    def encrypt(self, plaintext: str, *, workspace_id: UUID) -> bytes: ...

    def decrypt(self, blob: bytes, *, workspace_id: UUID) -> str: ...

    def rotate(self, blob: bytes, *, workspace_id: UUID) -> bytes: ...


@runtime_checkable
class KeyProvider(Protocol):
    """Versioned 32-byte KEK source (env-backed in prod, fixed-key in tests)."""

    def get(self, version: int) -> bytes: ...

    def active_version(self) -> int: ...


@runtime_checkable
class RateLimiter(Protocol):
    """Per-workspace / per-user / per-key request limiter."""

    async def check(self, key: str, *, limit: int, window_seconds: int) -> RateDecision: ...


@runtime_checkable
class SecretRedactor(Protocol):
    """Canonical secret scrubber applied to logs, traces, and audit metadata."""

    def redact(self, text: str) -> str: ...

    def register_known_secret(self, value: str) -> None: ...
