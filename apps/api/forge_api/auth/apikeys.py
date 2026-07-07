"""Forge API-key authentication store (Task 1.15 — auth & secrets).

Spec Security: "Auth — OAuth + API key; all routes authenticated". Forge-issued
API keys authenticate a client (a user, CI job, or agent-runner) to the Forge
API. Only a keyed one-way hash (HMAC-SHA256 under the instance secret) is stored;
the plaintext token is returned exactly once at mint time and is otherwise
unrecoverable. Verification is constant-time and honours revocation + expiry
(spec: "automatic expiry for agent tokens").

The store is in-memory for hermetic Phase-1 tests; a Postgres-backed store can be
swapped in at the :class:`APIKeyBackend` boundary during Phase-2.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from forge_contracts.enums import APIKeyKind, UserRole

#: Number of random bytes of entropy in a minted token (256 bits).
_TOKEN_BYTES = 32
#: Leading characters retained for display (``forge_<kind>_<8 chars>…``).
_PREFIX_VISIBLE = 8


def generate_api_token(kind: APIKeyKind) -> str:
    """Return a fresh opaque API token of the form ``forge_<kind>_<random>``."""
    return f"forge_{kind.value}_{secrets.token_urlsafe(_TOKEN_BYTES)}"


def _token_prefix(token: str) -> str:
    """A short, display-safe prefix used to index candidate keys without the hash."""
    return token[:_PREFIX_VISIBLE]


class APIKeyRecord(BaseModel):
    """The stored API-key record. Holds only a one-way ``token_hash``."""

    model_config = ConfigDict(frozen=False)

    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    kind: APIKeyKind
    role: UserRole
    key_prefix: str
    token_hash: str
    user_id: uuid.UUID | None = None
    created_at: datetime
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    is_active: bool = True


class APIKeyInfo(BaseModel):
    """Redacted, serialization-safe view of an API key (no token, no hash)."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    kind: APIKeyKind
    role: UserRole
    key_prefix: str
    user_id: uuid.UUID | None = None
    created_at: datetime
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    is_active: bool = True


def _to_info(record: APIKeyRecord) -> APIKeyInfo:
    return APIKeyInfo(
        id=record.id,
        workspace_id=record.workspace_id,
        name=record.name,
        kind=record.kind,
        role=record.role,
        key_prefix=record.key_prefix,
        user_id=record.user_id,
        created_at=record.created_at,
        last_used_at=record.last_used_at,
        expires_at=record.expires_at,
        is_active=record.is_active,
    )


@runtime_checkable
class APIKeyBackend(Protocol):
    """Storage boundary for API-key records (Phase-2 may back with Postgres)."""

    def add(self, record: APIKeyRecord) -> None: ...

    def by_prefix(self, prefix: str) -> list[APIKeyRecord]: ...

    def list(self, workspace_id: uuid.UUID) -> list[APIKeyRecord]: ...

    def get(self, workspace_id: uuid.UUID, key_id: uuid.UUID) -> APIKeyRecord | None: ...


class InMemoryAPIKeyBackend:
    """Hermetic in-memory API-key backend indexed by id and token prefix."""

    def __init__(self) -> None:
        self._by_id: dict[uuid.UUID, APIKeyRecord] = {}

    def add(self, record: APIKeyRecord) -> None:
        self._by_id[record.id] = record

    def by_prefix(self, prefix: str) -> list[APIKeyRecord]:
        return [r for r in self._by_id.values() if r.key_prefix == prefix]

    def list(self, workspace_id: uuid.UUID) -> list[APIKeyRecord]:
        return [r for r in self._by_id.values() if r.workspace_id == workspace_id]

    def get(self, workspace_id: uuid.UUID, key_id: uuid.UUID) -> APIKeyRecord | None:
        record = self._by_id.get(key_id)
        if record is None or record.workspace_id != workspace_id:
            return None
        return record


class APIKeyStore:
    """Mint, verify, list, and revoke Forge API keys."""

    def __init__(self, *, secret_key: bytes, backend: APIKeyBackend | None = None) -> None:
        if len(secret_key) < 16:
            raise ValueError("secret_key must be at least 16 bytes")
        self._secret = bytes(secret_key)
        self._backend: APIKeyBackend = backend or InMemoryAPIKeyBackend()

    def _hash(self, token: str) -> str:
        return hmac.new(self._secret, token.encode("utf-8"), hashlib.sha256).hexdigest()

    def mint(
        self,
        *,
        workspace_id: uuid.UUID,
        name: str,
        role: UserRole,
        kind: APIKeyKind = APIKeyKind.SYSTEM,
        user_id: uuid.UUID | None = None,
        expires_at: datetime | None = None,
    ) -> tuple[APIKeyInfo, str]:
        """Create a key; returns ``(redacted_info, plaintext_token)`` — token once."""
        token = generate_api_token(kind)
        record = APIKeyRecord(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            name=name,
            kind=kind,
            role=role,
            key_prefix=_token_prefix(token),
            token_hash=self._hash(token),
            user_id=user_id,
            created_at=datetime.now(UTC),
            expires_at=expires_at,
        )
        self._backend.add(record)
        return _to_info(record), token

    def verify(self, token: str) -> APIKeyRecord | None:
        """Return the matching active, unexpired record, else ``None`` (constant-time)."""
        candidate_hash = self._hash(token)
        match: APIKeyRecord | None = None
        for record in self._backend.by_prefix(_token_prefix(token)):
            if hmac.compare_digest(record.token_hash, candidate_hash):
                match = record
                break
        if match is None or not match.is_active:
            return None
        if match.expires_at is not None and match.expires_at <= datetime.now(UTC):
            return None
        match.last_used_at = datetime.now(UTC)
        return match

    def list_keys(self, workspace_id: uuid.UUID) -> list[APIKeyInfo]:
        return [_to_info(r) for r in self._backend.list(workspace_id)]

    def revoke(self, workspace_id: uuid.UUID, key_id: uuid.UUID) -> bool:
        record = self._backend.get(workspace_id, key_id)
        if record is None:
            return False
        record.is_active = False
        return True

    def revoke_for_user(self, workspace_id: uuid.UUID, user_id: uuid.UUID) -> int:
        """Revoke every active key bound to ``user_id`` (F33 deprovision hook).

        This is the F37 session-layer ``revoke_all_for_user`` seam the SSO slice
        calls on SCIM ``active=false`` / ``DELETE`` so a removed employee loses
        API/agent access immediately. Returns the number of keys revoked.
        """
        revoked = 0
        for record in self._backend.list(workspace_id):
            if record.user_id == user_id and record.is_active:
                record.is_active = False
                revoked += 1
        return revoked


__all__ = [
    "APIKeyBackend",
    "APIKeyInfo",
    "APIKeyRecord",
    "APIKeyStore",
    "InMemoryAPIKeyBackend",
    "generate_api_token",
]
