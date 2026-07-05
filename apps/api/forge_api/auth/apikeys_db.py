"""Postgres-backed platform API-key backend (Phase-2 persistence).

:class:`DbAPIKeyBackend` is a drop-in, durable alternative to
:class:`~forge_api.auth.apikeys.InMemoryAPIKeyBackend` that satisfies the **same**
:class:`~forge_api.auth.apikeys.APIKeyBackend` seam (``add`` / ``by_prefix`` /
``list`` / ``get``) the :class:`~forge_api.auth.apikeys.APIKeyStore` mints, verifies,
lists, and revokes through. The composition root swaps it in behind
``FORGE_APIKEY_BACKEND=db``; the default stays ``memory`` and the in-memory store
remains the unit-test default, so no existing behaviour changes.

It maps the domain :class:`~forge_api.auth.apikeys.APIKeyRecord` onto the canonical
``platform_api_key`` ORM row (``PlatformAPIKey``, created by migration 0020, F37) —
so **no new migration** is required. Two storage-boundary details are load-bearing:

* **Enum taxonomy.** The record carries an
  :class:`~forge_contracts.enums.APIKeyKind` (BYOK-flavoured; the store only ever
  mints ``SYSTEM`` for platform auth), while the frozen ``platform_api_key.kind``
  column is a :class:`~forge_contracts.auth.PlatformKeyKind`. The two are bridged
  by :data:`_KIND_TO_PLATFORM` / :data:`_PLATFORM_TO_KIND`, a documented mapping
  that round-trips the platform-auth kinds (``SYSTEM`` ⇄ ``service``,
  ``MODEL_PROVIDER`` ⇄ ``personal``) verbatim and never emits ``agent_runner`` on
  write (so the ``agent_runner ⇒ expires_at`` CHECK is never tripped). The two
  BYOK-only kinds that never reach the platform-key store fold onto those two
  slots and read back as their canonical twin (documented, unreachable in practice).

* **Mutation through returned records.** ``APIKeyStore.revoke`` /
  ``revoke_for_user`` flip ``is_active`` and ``verify`` stamps ``last_used_at`` by
  mutating the record object the backend returns — the in-memory store persists
  that only because it hands back live references. To preserve that behaviour
  exactly, this backend returns :class:`_LiveAPIKeyRecord` instances that
  write-through those two fields (``is_active`` → ``revoked_at``; ``last_used_at``)
  to the row on assignment. Revocation and last-used tracking therefore work
  identically on both backends.
"""

from __future__ import annotations

import builtins
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import PrivateAttr
from sqlalchemy import select, update

from forge_api.auth.apikeys import APIKeyRecord
from forge_contracts.auth import PlatformKeyKind
from forge_contracts.enums import APIKeyKind, UserRole
from forge_db.models import PlatformAPIKey
from forge_db.models.enums import UserRole as DbUserRole

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

__all__ = ["DbAPIKeyBackend"]

#: Forward map: the record's :class:`APIKeyKind` → the column's
#: :class:`PlatformKeyKind`. ``agent_runner`` is deliberately never a target so the
#: ``agent_runner ⇒ expires_at`` CHECK is never at risk; the platform-auth kinds
#: (``SYSTEM``/``MODEL_PROVIDER``) map onto distinct slots and round-trip verbatim.
_KIND_TO_PLATFORM: dict[APIKeyKind, PlatformKeyKind] = {
    APIKeyKind.SYSTEM: PlatformKeyKind.SERVICE,
    APIKeyKind.MODEL_PROVIDER: PlatformKeyKind.PERSONAL,
    APIKeyKind.INTEGRATION_TOKEN: PlatformKeyKind.SERVICE,
    APIKeyKind.MCP_TOKEN: PlatformKeyKind.PERSONAL,
}

#: Reverse map for reads. Total over :class:`PlatformKeyKind`; ``agent_runner`` is
#: covered defensively (this backend never writes it) and reads back as ``SYSTEM``.
_PLATFORM_TO_KIND: dict[PlatformKeyKind, APIKeyKind] = {
    PlatformKeyKind.SERVICE: APIKeyKind.SYSTEM,
    PlatformKeyKind.PERSONAL: APIKeyKind.MODEL_PROVIDER,
    PlatformKeyKind.AGENT_RUNNER: APIKeyKind.SYSTEM,
}

#: Record fields whose in-place mutation the store relies on the backend to
#: persist (the in-memory store gets this free via shared references).
_WRITE_THROUGH_FIELDS = frozenset({"is_active", "last_used_at"})


def _aware(value: datetime | None) -> datetime | None:
    """Normalise a stored timestamp to timezone-aware UTC (defensive)."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class _LiveAPIKeyRecord(APIKeyRecord):
    """An :class:`APIKeyRecord` that write-throughs revoke/last-used mutations.

    ``APIKeyStore`` mutates ``is_active`` / ``last_used_at`` on the record objects
    the backend returns; the in-memory store persists those only because it hands
    back the very objects it stores. This subclass reproduces that behaviour for
    the DB backend by forwarding those two assignments to a persistence hook, so a
    ``revoke`` / ``revoke_for_user`` / ``verify`` behaves identically on both.
    """

    _persist: Callable[[uuid.UUID, str, Any], None] | None = PrivateAttr(default=None)

    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        private = getattr(self, "__pydantic_private__", None)
        hook = private.get("_persist") if private else None
        if hook is not None and name in _WRITE_THROUGH_FIELDS:
            hook(self.id, name, value)


class DbAPIKeyBackend:
    """A Postgres-backed API-key backend (implements ``APIKeyBackend``)."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory

    # ------------------------------------------------------------------ #
    # Mapping                                                             #
    # ------------------------------------------------------------------ #

    def _to_row(self, record: APIKeyRecord) -> PlatformAPIKey:
        """Build the ORM row for a domain record (kind/role/active translated)."""
        return PlatformAPIKey(
            id=record.id,
            workspace_id=record.workspace_id,
            name=record.name,
            # Non-secret unique lookup id the column requires (the record has no
            # ``key_id`` of its own); derived from the unique record id, stable
            # across re-adds and never surfaced back into the domain.
            key_id=record.id.hex[:16],
            key_hash=record.token_hash,
            key_prefix=record.key_prefix,
            kind=_KIND_TO_PLATFORM[record.kind],
            role=DbUserRole(record.role.value),
            created_by=record.user_id,
            created_at=record.created_at,
            last_used_at=record.last_used_at,
            expires_at=record.expires_at,
            revoked_at=None if record.is_active else datetime.now(UTC),
        )

    def _to_record(self, row: PlatformAPIKey) -> _LiveAPIKeyRecord:
        """Rebuild a live (write-through) domain record from a persisted row."""
        record = _LiveAPIKeyRecord(
            id=row.id,
            workspace_id=row.workspace_id,
            name=row.name,
            kind=_PLATFORM_TO_KIND[row.kind],
            role=UserRole(row.role.value),
            key_prefix=row.key_prefix,
            token_hash=row.key_hash,
            user_id=row.created_by,
            created_at=_aware(row.created_at),  # type: ignore[arg-type]
            last_used_at=_aware(row.last_used_at),
            expires_at=_aware(row.expires_at),
            is_active=row.revoked_at is None,
        )
        record._persist = self._persist_field
        return record

    # ------------------------------------------------------------------ #
    # Write-through persistence for in-place record mutation             #
    # ------------------------------------------------------------------ #

    def _persist_field(self, key_id: uuid.UUID, name: str, value: Any) -> None:
        """Persist a mutation of ``is_active`` / ``last_used_at`` on one row."""
        if name == "last_used_at":
            values: dict[str, Any] = {"last_used_at": value}
        else:  # is_active → revoked_at (kept for audit; None re-activates)
            values = {"revoked_at": None if value else datetime.now(UTC)}
        with self._sf() as session:
            session.execute(
                update(PlatformAPIKey).where(PlatformAPIKey.id == key_id).values(**values)
            )
            session.commit()

    # ------------------------------------------------------------------ #
    # APIKeyBackend seam                                                  #
    # ------------------------------------------------------------------ #

    def add(self, record: APIKeyRecord) -> None:
        """Persist a record; overwrites on a repeated id (mirrors the dict store)."""
        with self._sf() as session:
            session.merge(self._to_row(record))
            session.commit()

    def by_prefix(self, prefix: str) -> builtins.list[APIKeyRecord]:
        """Every record whose display prefix matches, oldest first (stable)."""
        with self._sf() as session:
            rows = session.scalars(
                select(PlatformAPIKey)
                .where(PlatformAPIKey.key_prefix == prefix)
                .order_by(PlatformAPIKey.created_at.asc(), PlatformAPIKey.id.asc())
            ).all()
            return [self._to_record(r) for r in rows]

    def list(self, workspace_id: uuid.UUID) -> builtins.list[APIKeyRecord]:
        """Every record in a workspace, oldest first (stable ordering)."""
        with self._sf() as session:
            rows = session.scalars(
                select(PlatformAPIKey)
                .where(PlatformAPIKey.workspace_id == workspace_id)
                .order_by(PlatformAPIKey.created_at.asc(), PlatformAPIKey.id.asc())
            ).all()
            return [self._to_record(r) for r in rows]

    def get(self, workspace_id: uuid.UUID, key_id: uuid.UUID) -> APIKeyRecord | None:
        """The record with ``key_id`` in ``workspace_id``, else ``None``."""
        with self._sf() as session:
            row = session.scalars(
                select(PlatformAPIKey).where(
                    PlatformAPIKey.id == key_id,
                    PlatformAPIKey.workspace_id == workspace_id,
                )
            ).first()
            return self._to_record(row) if row is not None else None
