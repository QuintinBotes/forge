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


class SecretExpiredError(SecretNotFoundError):
    """Raised when a stored secret has passed its ``expires_at`` (read-time expiry).

    Subclasses :class:`SecretNotFoundError` so existing callers that catch the
    latter keep failing closed, while HARD-13-aware callers can distinguish an
    expired credential (→ HTTP 409 "rotate this credential") from a missing one.
    """


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
    #: HARD-13 envelope bookkeeping: the KEK version the row's DEK is wrapped
    #: under, and when it was last re-wrapped (KEK rotation audit trail).
    key_version: int = 1
    rotated_at: datetime | None = None

    def __repr__(self) -> str:  # secret-safe
        return (
            f"StoredSecret(id={self.id!r}, workspace_id={self.workspace_id!r}, "
            f"name={self.name!r}, kind={self.kind!r}, provider={self.provider!r}, "
            f"key_prefix={self.key_prefix!r}, ciphertext=<{len(self.ciphertext)} bytes>)"
        )


def _is_expired(expires_at: datetime | None, now: datetime | None = None) -> bool:
    """True when ``expires_at`` is set and has passed (naive-safe comparison)."""
    if expires_at is None:
        return False
    reference = now or datetime.now(UTC)
    # Tolerate a naive ``expires_at`` (e.g. SQLite round-trips) by assuming UTC.
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    return expires_at <= reference


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
    #: Computed at serialisation time so the UI can badge an expired credential
    #: without re-deriving the rule; never a stored column (HARD-13 AC8).
    is_expired: bool = False


def _to_info(record: StoredSecret, *, now: datetime | None = None) -> SecretInfo:
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
        is_expired=_is_expired(record.expires_at, now),
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

    def get_secret(
        self,
        workspace_id: uuid.UUID,
        secret_id: uuid.UUID,
        *,
        now: datetime | None = None,
    ) -> str:
        """Decrypt and return a secret's plaintext (workspace-scoped).

        Enforces read-time expiry (HARD-13): a record past its ``expires_at``
        raises :class:`SecretExpiredError` rather than returning stale plaintext.
        The clock is injectable for deterministic tests. Use :meth:`raw_record`
        to obtain the (still-encrypted) ciphertext of an expired record for
        rotation.
        """
        record = self._store.get(workspace_id, secret_id)
        if record is None:
            raise SecretNotFoundError(secret_id)
        if _is_expired(record.expires_at, now):
            raise SecretExpiredError(secret_id)
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

    # -- rotation & expiry (HARD-13) --------------------------------------- #

    def rotate_secret(
        self,
        *,
        workspace_id: uuid.UUID,
        secret_id: uuid.UUID,
        new_secret: str,
        expires_at: datetime | None = None,
    ) -> SecretInfo:
        """Rotate a BYOK *value* in place: re-encrypt, keep id/name/kind.

        This is credential rotation (the operator supplies a new secret value),
        distinct from KEK rotation (:meth:`rewrap_all`, which never sees
        plaintext). Preserves the record identity, refreshes ``key_prefix`` and
        ``updated_at``, and optionally sets a new ``expires_at``.
        """
        record = self._store.get(workspace_id, secret_id)
        if record is None:
            raise SecretNotFoundError(secret_id)
        record.ciphertext = self._cipher.encrypt(new_secret)
        record.key_prefix = _key_prefix(new_secret)
        record.updated_at = datetime.now(UTC)
        if expires_at is not None:
            record.expires_at = expires_at
        self._store.add(record)
        return _to_info(record)

    def rewrap_all(
        self,
        *,
        keyring: object,
        to_version: int | None = None,
    ) -> dict[str, int]:
        """Re-wrap every stored DEK under the current (or ``to_version``) KEK.

        KEK rotation: the data ciphertext is preserved for ``\\x02`` rows (only
        the wrapped DEK is re-encrypted), so no BYOK plaintext is decrypted.
        Requires the vault's cipher to be an :class:`EnvelopeCipher`. Returns
        ``{"rewrapped": N, "skipped": M}``; a row already at ``to_version`` is
        skipped.
        """
        from forge_api.auth.crypto import EnvelopeCipher

        cipher = self._cipher
        if not isinstance(cipher, EnvelopeCipher):
            raise TypeError(
                "rewrap_all requires an EnvelopeCipher-backed vault "
                "(FORGE_ENVELOPE_ENCRYPTION must be enabled)."
            )
        target = keyring.current_version if to_version is None else to_version  # type: ignore[attr-defined]
        rewrapped = 0
        skipped = 0
        # Iterate every workspace's records via the store's internal map. The
        # in-memory store exposes per-workspace listing; rewrap spans all rows.
        for record in self._all_records():
            if EnvelopeCipher.kek_version(record.ciphertext) == target:
                skipped += 1
                continue
            new_blob, _ = cipher.rewrap(record.ciphertext, to_version=target)
            record.ciphertext = new_blob
            record.key_version = target
            record.rotated_at = datetime.now(UTC)
            self._store.add(record)
            rewrapped += 1
        return {"rewrapped": rewrapped, "skipped": skipped}

    def sweep_expired(self, *, now: datetime | None = None, purge: bool = False) -> int:
        """Flag (or, with ``purge``, delete) records past ``expires_at``.

        Returns the number of expired records found. Read-time expiry is already
        authoritative (:meth:`get_secret` raises), so this is hygiene: a missed
        run never surfaces an expired secret. With ``purge=True`` the expired
        rows are removed from the store.
        """
        reference = now or datetime.now(UTC)
        expired = [r for r in self._all_records() if _is_expired(r.expires_at, reference)]
        if purge:
            for record in expired:
                self._store.remove(record.workspace_id, record.id)
        return len(expired)

    def _all_records(self) -> list[StoredSecret]:
        """Return every stored record across workspaces (rotation/sweep helper)."""
        by_id = getattr(self._store, "_by_id", None)
        if by_id is not None:
            return list(by_id.values())
        # A store without the in-memory internal map must expose ``all_records``.
        all_records = getattr(self._store, "all_records", None)
        if callable(all_records):
            return list(all_records())
        raise TypeError(
            "rewrap_all/sweep_expired require a store exposing all records "
            "(InMemorySecretStore or an all_records() method)."
        )


__all__ = [
    "InMemorySecretStore",
    "SecretExpiredError",
    "SecretInfo",
    "SecretNotFoundError",
    "SecretStore",
    "SecretVault",
    "StoredSecret",
]
