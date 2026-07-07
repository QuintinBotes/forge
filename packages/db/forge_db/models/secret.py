"""Durable backing table for the encrypted BYOK secret vault (secret-vault persist).

The API's :class:`forge_api.auth.vault.InMemorySecretStore` is the storage
boundary behind :class:`~forge_api.auth.vault.SecretVault`: it holds
:class:`~forge_api.auth.vault.StoredSecret` records — the *envelope-encrypted*
credential (``ciphertext``), its :class:`~forge_contracts.enums.APIKeyKind`, the
HARD-13 envelope ``key_version`` the row's DEK is wrapped under, an optional
``expires_at`` (read-time expiry), and the owning ``workspace_id`` (per-workspace
isolation). This module is the Postgres backing for the *db* variant of that
store (``forge_api.auth.vault_db.DbSecretStore``): one row per stored secret.

Why a **new** table rather than reusing ``api_key``: the two are genuinely
different concerns. ``api_key`` (F37 ``APIKey``) is the ORM row a *different*
BYOK code path already owns end-to-end; ``platform_api_key`` is inbound,
verify-only auth. This ``secret`` table is the durable image of the vault's
``StoredSecret`` boundary and nothing else, so the vault store can round-trip its
record verbatim without colliding with either existing table's invariants.

Storage-boundary fidelity (load-bearing — the vault must round-trip exactly):

* ``ciphertext`` is the opaque, envelope-encrypted blob (``LargeBinary``); the
  plaintext is **never** persisted and ``__repr__`` never reveals the ciphertext.
* ``kind`` stores the :class:`APIKeyKind` value verbatim (VARCHAR + CHECK via the
  shared :func:`enum_type`), byte-compatible with ``forge_contracts``.
* ``key_version`` / ``rotated_at`` mirror the HARD-13 envelope bookkeeping on
  ``api_key`` (0023): the KEK version the DEK is wrapped under, and when it was
  last re-wrapped — so KEK rotation (``rewrap_all``) can target
  ``WHERE key_version < :current`` cheaply.
* ``created_at`` / ``updated_at`` are the domain record's own timestamps (the
  repository persists them explicitly rather than letting the DB clock win), so a
  round-tripped record equals the one the vault stored.
* ``secret_metadata`` is a reserved JSONB bag (defaults to ``{}``) for
  forward-compatible per-secret annotations; the current ``StoredSecret`` carries
  none, so it stays empty and never holds a credential.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Index,
    LargeBinary,
    SmallInteger,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from forge_db.base import WorkspaceScopedModel, enum_type, json_type
from forge_db.models.enums import APIKeyKind


class Secret(WorkspaceScopedModel):
    """One persisted, envelope-encrypted BYOK secret (the vault's ``StoredSecret``).

    Tenant-scoped like every credential row (``workspace_id`` FK, CASCADE) so the
    per-workspace isolation the vault enforces in code is also enforced by the
    schema. The primary key is the domain record's own ``id`` (client-generated in
    :meth:`SecretVault.put_secret`), preserved verbatim across the round-trip.
    """

    __tablename__ = "secret"
    __table_args__ = (
        # Envelope-KEK rotation targets ``WHERE key_version < :current`` (0023 parity).
        Index("ix_secret_key_version", "key_version"),
        # Expiry sweep / read-time-expiry queries touch only rows that can expire.
        Index(
            "ix_secret_expires_at",
            "expires_at",
            postgresql_where=text("expires_at IS NOT NULL"),
        ),
    )

    #: Human label for the credential (unique-per-workspace is a caller concern,
    #: not enforced here — the in-memory store imposes no such constraint).
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    #: BYOK key taxonomy; stored as the enum ``value`` (byte-compatible with contracts).
    kind: Mapped[APIKeyKind] = mapped_column(enum_type(APIKeyKind), nullable=False)
    #: Optional provider label (e.g. ``anthropic``); redacted view only.
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Short display-safe prefix (e.g. ``sk-a…``); never the credential itself.
    key_prefix: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: The opaque envelope-encrypted credential blob (no plaintext, ever).
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    #: Refreshed when the plaintext is decrypted for use (optional).
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: NULL = no expiry; a past value ⇒ the vault raises ``SecretExpiredError``.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: HARD-13 envelope bookkeeping: the KEK version the row's DEK is wrapped under.
    key_version: Mapped[int] = mapped_column(
        SmallInteger, default=1, server_default=text("1"), nullable=False
    )
    #: When the DEK was last re-wrapped under a newer KEK (KEK-rotation audit).
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Reserved forward-compat annotation bag (JSONB on Postgres); currently ``{}``.
    secret_metadata: Mapped[dict[str, Any]] = mapped_column(
        json_type(), default=dict, nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - trivial, secret-safe
        return (
            f"Secret(id={self.id!r}, workspace_id={self.workspace_id!r}, "
            f"name={self.name!r}, kind={self.kind!r}, provider={self.provider!r}, "
            f"key_prefix={self.key_prefix!r}, ciphertext=<redacted>)"
        )


__all__ = ["Secret"]
