"""Postgres-backed encrypted secret store (secret-vault persistence).

:class:`DbSecretStore` is a drop-in, durable alternative to
:class:`~forge_api.auth.vault.InMemorySecretStore` that satisfies the **same**
:class:`~forge_api.auth.vault.SecretStore` protocol (``add`` / ``get`` / ``list``
/ ``remove``) the :class:`~forge_api.auth.vault.SecretVault` stores, reads,
lists, and rotates through. The composition root swaps it in behind
``FORGE_SECRET_BACKEND=db``; the default stays ``memory`` and the in-memory store
remains the unit-test default, so no existing behaviour changes.

It maps the domain :class:`~forge_api.auth.vault.StoredSecret` onto the ``secret``
ORM row one-to-one, preserving every field verbatim so a round-tripped record
equals the one the vault stored:

* **Envelope encryption stays opaque.** ``ciphertext`` is persisted as the exact
  bytes the cipher produced; the plaintext is never decrypted here, never logged,
  and the ``__repr__`` on both the record and the row hides it. The vault's
  read-time expiry (``SecretExpiredError``) and rotation (``rewrap_all`` via
  ``all_records``) work unchanged because ``expires_at`` / ``key_version`` /
  ``rotated_at`` round-trip faithfully.
* **Not-found / cross-tenant semantics.** ``get`` returns ``None`` (never raises)
  for a missing *or* cross-workspace id, exactly like the in-memory store — so the
  vault raises ``SecretNotFoundError`` at its boundary identically on both
  backends. ``remove`` returns ``False`` for the same cases.
* **Domain-owned timestamps.** ``created_at`` / ``updated_at`` are persisted from
  the record (not the DB clock), and the update path writes ``updated_at``
  explicitly so the column's ``onupdate`` never overrides a rewrap that left
  ``updated_at`` untouched — the in-memory store mutates in place and keeps the
  old value, and this reproduces that byte-for-byte.

``add`` is an upsert (mirrors the in-memory ``dict[id] = record``): a fresh id
inserts, a repeated id updates in place, so ``rotate_secret`` / ``rewrap_all``
(which re-``add`` a mutated record) persist correctly. ``all_records`` is the
cross-workspace listing the vault's ``rewrap_all`` / ``sweep_expired`` helpers
require of a non-in-memory store.
"""

from __future__ import annotations

import builtins
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select, update

from forge_api.auth.vault import StoredSecret
from forge_contracts.enums import APIKeyKind
from forge_db.models import Secret
from forge_db.models.enums import APIKeyKind as DbAPIKeyKind

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

__all__ = ["DbSecretStore"]


def _aware(value: datetime | None) -> datetime | None:
    """Normalise a stored timestamp to timezone-aware UTC (defensive).

    A ``timestamptz`` reads back aware; a naive value (e.g. a SQLite round-trip)
    is assumed UTC — matching the vault's own ``_is_expired`` tolerance so
    read-time expiry stays correct on every dialect.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class DbSecretStore:
    """A Postgres-backed encrypted secret store (implements ``SecretStore``)."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory

    # ------------------------------------------------------------------ #
    # Mapping                                                             #
    # ------------------------------------------------------------------ #

    def _mutable_values(self, record: StoredSecret) -> dict[str, Any]:
        """Column kwargs for every field except the identity/created_at anchors."""
        return {
            "workspace_id": record.workspace_id,
            "name": record.name,
            "kind": DbAPIKeyKind(record.kind.value),
            "provider": record.provider,
            "key_prefix": record.key_prefix,
            "ciphertext": record.ciphertext,
            "last_used_at": record.last_used_at,
            "expires_at": record.expires_at,
            "key_version": record.key_version,
            "rotated_at": record.rotated_at,
        }

    def _to_record(self, row: Secret) -> StoredSecret:
        """Rebuild the exact :class:`StoredSecret` that produced ``row``."""
        return StoredSecret(
            id=row.id,
            workspace_id=row.workspace_id,
            name=row.name,
            kind=APIKeyKind(row.kind),
            ciphertext=bytes(row.ciphertext),
            provider=row.provider,
            key_prefix=row.key_prefix,
            created_at=_aware(row.created_at),  # type: ignore[arg-type]
            updated_at=_aware(row.updated_at),  # type: ignore[arg-type]
            last_used_at=_aware(row.last_used_at),
            expires_at=_aware(row.expires_at),
            key_version=row.key_version,
            rotated_at=_aware(row.rotated_at),
        )

    # ------------------------------------------------------------------ #
    # SecretStore protocol                                               #
    # ------------------------------------------------------------------ #

    def add(self, record: StoredSecret) -> None:
        """Persist a record; upsert on a repeated id (mirrors the dict store).

        A repeated id is an in-place update (``rotate_secret`` / ``rewrap_all``);
        ``updated_at`` is written explicitly so the column's ``onupdate`` never
        overrides a rewrap that deliberately left it unchanged.
        """
        with self._sf() as session:
            exists = session.get(Secret, record.id) is not None
            if exists:
                session.execute(
                    update(Secret)
                    .where(Secret.id == record.id)
                    .values(updated_at=record.updated_at, **self._mutable_values(record))
                )
            else:
                session.add(
                    Secret(
                        id=record.id,
                        created_at=record.created_at,
                        updated_at=record.updated_at,
                        secret_metadata={},
                        **self._mutable_values(record),
                    )
                )
            session.commit()

    def get(
        self, workspace_id: uuid.UUID, secret_id: uuid.UUID
    ) -> StoredSecret | None:
        """The record with ``secret_id`` in ``workspace_id``, else ``None``.

        Returns ``None`` (never raises) for a missing *or* cross-workspace id, so
        the vault raises ``SecretNotFoundError`` identically to the in-memory store.
        """
        with self._sf() as session:
            row = session.get(Secret, secret_id)
            if row is None or row.workspace_id != workspace_id:
                return None
            return self._to_record(row)

    def list(self, workspace_id: uuid.UUID) -> builtins.list[StoredSecret]:
        """Every record in a workspace, oldest first (stable ordering)."""
        with self._sf() as session:
            rows = session.scalars(
                select(Secret)
                .where(Secret.workspace_id == workspace_id)
                .order_by(Secret.created_at.asc(), Secret.id.asc())
            ).all()
            return [self._to_record(r) for r in rows]

    def remove(self, workspace_id: uuid.UUID, secret_id: uuid.UUID) -> bool:
        """Delete a workspace-scoped record; ``False`` if absent/cross-tenant."""
        with self._sf() as session:
            result = session.execute(
                delete(Secret).where(
                    Secret.id == secret_id,
                    Secret.workspace_id == workspace_id,
                )
            )
            session.commit()
            return bool(result.rowcount)

    # ------------------------------------------------------------------ #
    # Rotation / sweep helper (not part of the protocol)                 #
    # ------------------------------------------------------------------ #

    def all_records(self) -> builtins.list[StoredSecret]:
        """Every record across all workspaces (vault rewrap/sweep helper).

        The vault's ``rewrap_all`` / ``sweep_expired`` span every tenant; a
        non-in-memory store must expose this so KEK rotation and expiry hygiene
        work identically to the in-memory ``_by_id`` iteration.
        """
        with self._sf() as session:
            rows = session.scalars(
                select(Secret).order_by(Secret.created_at.asc(), Secret.id.asc())
            ).all()
            return [self._to_record(r) for r in rows]
