"""Encrypted BYOK secret vault (Task 1.15 — auth & secrets).

Spec Security: "Secrets — Encrypted at rest, per-workspace isolation". The vault
stores model-provider / integration / MCP credentials encrypted via a
:class:`~forge_api.auth.crypto.SecretCipher`. The plaintext is never persisted,
never returned in list/info views, and never appears in ``repr``.

The store is in-memory so Phase-1 unit tests stay hermetic; a Postgres-backed
store (the ``api_key`` table's ``encrypted_secret`` column already exists in
``forge_db``) can be swapped in at the :class:`SecretStore` boundary during
Phase-2 integration without changing the public surface.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from forge_api.auth.crypto import SecretCipher
from forge_contracts.enums import APIKeyKind


class SecretNotFoundError(KeyError):
    """Raised when a secret id is absent in the given workspace (also cross-tenant)."""


def _key_prefix(secret: str, *, visible: int = 4) -> str | None:
    """A short, display-safe prefix of a secret (e.g. ``sk-a…``).

    Reveals at most ``visible`` leading characters so the UI can disambiguate
    keys without exposing the credential. Returns ``None`` for trivially short
    secrets where even a prefix would leak too much.
    """
    if len(secret) <= visible:
        return None
    return secret[:visible] + "…"


@dataclass
class StoredSecret:
    """The persisted, encrypted secret record. Holds *no* plaintext."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    kind: APIKeyKind
    ciphertext: bytes
    provider: str | None = None
    key_prefix: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_used_at: datetime | None = None
    expires_at: datetime | None = None

    def __repr__(self) -> str:  # secret-safe
        return (
            f"StoredSecret(id={self.id!r}, workspace_id={self.workspace_id!r}, "
            f"name={self.name!r}, kind={self.kind!r}, provider={self.provider!r}, "
            f"key_prefix={self.key_prefix!r}, ciphertext=<{len(self.ciphertext)} bytes>)"
        )


class SecretInfo(BaseModel):
    """Redacted, serialization-safe view of a stored secret (no plaintext)."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    kind: APIKeyKind
    provider: str | None = None
    key_prefix: str | None = None
    created_at: datetime
    last_used_at: datetime | None = None
    expires_at: datetime | None = None


def _to_info(record: StoredSecret) -> SecretInfo:
    return SecretInfo(
        id=record.id,
        workspace_id=record.workspace_id,
        name=record.name,
        kind=record.kind,
        provider=record.provider,
        key_prefix=record.key_prefix,
        created_at=record.created_at,
        last_used_at=record.last_used_at,
        expires_at=record.expires_at,
    )


@runtime_checkable
class SecretStore(Protocol):
    """Storage boundary for encrypted secrets (Phase-2 may back with Postgres)."""

    def add(self, record: StoredSecret) -> None: ...

    def get(self, workspace_id: uuid.UUID, secret_id: uuid.UUID) -> StoredSecret | None: ...

    def list(self, workspace_id: uuid.UUID) -> list[StoredSecret]: ...

    def remove(self, workspace_id: uuid.UUID, secret_id: uuid.UUID) -> bool: ...


class InMemorySecretStore:
    """Hermetic in-memory secret store with strict per-workspace scoping."""

    def __init__(self) -> None:
        self._by_id: dict[uuid.UUID, StoredSecret] = {}

    def add(self, record: StoredSecret) -> None:
        self._by_id[record.id] = record

    def get(self, workspace_id: uuid.UUID, secret_id: uuid.UUID) -> StoredSecret | None:
        record = self._by_id.get(secret_id)
        if record is None or record.workspace_id != workspace_id:
            return None
        return record

    def list(self, workspace_id: uuid.UUID) -> list[StoredSecret]:
        return [r for r in self._by_id.values() if r.workspace_id == workspace_id]

    def remove(self, workspace_id: uuid.UUID, secret_id: uuid.UUID) -> bool:
        record = self._by_id.get(secret_id)
        if record is None or record.workspace_id != workspace_id:
            return False
        del self._by_id[secret_id]
        return True


class SecretVault:
    """Encrypt-at-rest BYOK vault with per-workspace isolation."""

    def __init__(self, *, cipher: SecretCipher, store: SecretStore | None = None) -> None:
        self._cipher = cipher
        self._store: SecretStore = store or InMemorySecretStore()

    def put_secret(
        self,
        *,
        workspace_id: uuid.UUID,
        name: str,
        secret: str,
        kind: APIKeyKind,
        provider: str | None = None,
        expires_at: datetime | None = None,
    ) -> SecretInfo:
        """Encrypt and store a secret; returns the redacted :class:`SecretInfo`."""
        record = StoredSecret(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            name=name,
            kind=kind,
            provider=provider,
            key_prefix=_key_prefix(secret),
            ciphertext=self._cipher.encrypt(secret),
            expires_at=expires_at,
        )
        self._store.add(record)
        return _to_info(record)

    def get_secret(self, workspace_id: uuid.UUID, secret_id: uuid.UUID) -> str:
        """Decrypt and return a secret's plaintext (workspace-scoped)."""
        record = self._store.get(workspace_id, secret_id)
        if record is None:
            raise SecretNotFoundError(secret_id)
        return self._cipher.decrypt(record.ciphertext)

    def raw_record(self, workspace_id: uuid.UUID, secret_id: uuid.UUID) -> StoredSecret:
        """Return the encrypted record (for persistence / inspection; no plaintext)."""
        record = self._store.get(workspace_id, secret_id)
        if record is None:
            raise SecretNotFoundError(secret_id)
        return record

    def list_secrets(self, workspace_id: uuid.UUID) -> list[SecretInfo]:
        return [_to_info(r) for r in self._store.list(workspace_id)]

    def delete_secret(self, workspace_id: uuid.UUID, secret_id: uuid.UUID) -> None:
        if not self._store.remove(workspace_id, secret_id):
            raise SecretNotFoundError(secret_id)


__all__ = [
    "InMemorySecretStore",
    "SecretInfo",
    "SecretNotFoundError",
    "SecretStore",
    "SecretVault",
    "StoredSecret",
]
