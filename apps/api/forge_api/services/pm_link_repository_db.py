"""Postgres-backed :class:`~forge_integrations.pm.sync_engine.LinkRepository` (F18).

:class:`DbLinkRepository` is a drop-in, durable alternative to
:class:`~forge_integrations.pm.sync_engine.InMemoryLinkRepository` that satisfies
the **same** ``LinkRepository`` protocol (``get`` / ``get_by_forge_task`` /
``get_by_external`` / ``upsert`` / ``delete`` / ``list_by_state``) — so the F18
composition root swaps it in behind ``FORGE_PM_LINK_BACKEND=db`` with no
behavioural change. The default stays ``memory`` and the in-memory store remains
the unit-test default (``PMSyncEngine`` is unit-tested against the in-memory
seam), so every existing sync-engine test stays green untouched.

It lives in ``apps/api`` (not the ``forge_integrations`` SDK, which is
deliberately DB-free — the sync-engine module docstring says the engine "can be
wired to the real board service or to in-memory fakes"), exactly like the
sibling :class:`~forge_api.services.approval_repository_db.SqlAlchemyApprovalRepository`.
It maps the engine's :class:`~forge_integrations.pm.sync_engine.LinkRecord` onto
the canonical F18 ORM row (``forge_db.models.PMTaskLink`` / table
``pm_task_link``, migration ``0002``) — the very table the API-side
``PMConnectionService`` already reads, so a ``db``-backed engine and the API
service share one durable link store.

Behaviour parity with the in-memory store is exact and intentional:

* ``upsert`` is keyed by ``LinkRecord.id`` (the row primary key): an unknown id
  inserts, a known id rewrites every field — the DB analogue of the in-memory
  ``self._by_id[link.id] = link.model_copy(deep=True)``, and it returns the
  stored record verbatim (a deep copy);
* every read maps the persisted row back to a fresh ``LinkRecord`` (never a live
  ORM object), mirroring the in-memory store's ``model_copy(deep=True)`` so a
  mutation of a returned record never leaks back into the store;
* ``get_by_forge_task`` / ``get_by_external`` scope by ``connection_id`` and read
  as absent (``None``) for an unknown key; ``list_by_state`` filters by
  ``(connection_id, sync_state)`` and returns rows in a deterministic order.

The contracts enums the ``LinkRecord`` carries (``PMProvider`` / ``PMSyncState``
from ``forge_contracts.pm``) are value-identical to the DB enums the row stores
(``forge_db.models.enums``), so the mapping is a straight ``Enum(value)`` on each
boundary.

Storage-boundary divergences (shared with every DB-backed repo here; both still
satisfy the same protocol):

* ``workspace_id`` / ``connection_id`` / ``forge_task_id`` are **real** foreign
  keys, so an ``upsert`` requires those parent rows (``workspace`` /
  ``pm_connection`` / ``task``) to exist; the engine only ever links tasks/
  connections it has just written, so this never surfaces in normal flow;
* the ``(connection_id, external_id)`` and ``(connection_id, forge_task_id)``
  unique constraints are enforced by the database — the engine always resolves an
  existing link via ``get_by_forge_task`` / ``get_by_external`` before creating a
  new ``LinkRecord``, so the wholesale upsert never trips them in normal flow, but
  a direct duplicate insert (a *different* id colliding on one of those pairs)
  raises at the boundary rather than silently succeeding.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from forge_contracts.pm import PMProvider as ContractsProvider
from forge_contracts.pm import PMSyncState as ContractsSyncState
from forge_db.models import PMTaskLink
from forge_db.models.enums import PMProvider as DbProvider
from forge_db.models.enums import PMSyncState as DbSyncState
from forge_integrations.pm.sync_engine import LinkRecord

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy.orm import Session, sessionmaker

__all__ = ["DbLinkRepository"]


def _aware(value: datetime | None) -> datetime | None:
    """Normalise a stored timestamp to timezone-aware UTC (SQLite reads naive)."""
    if value is None:
        return None
    from datetime import UTC

    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _db_provider(value: ContractsProvider) -> DbProvider:
    """Contracts ``PMProvider`` -> the DB ``PMProvider`` (value-identical enums)."""
    return DbProvider(value.value)


def _db_state(value: ContractsSyncState) -> DbSyncState:
    """Contracts ``PMSyncState`` -> the DB ``PMSyncState`` (value-identical enums)."""
    return DbSyncState(value.value)


def _contracts_provider(value: object) -> ContractsProvider:
    """DB ``PMProvider`` (or raw string) -> the contracts :class:`PMProvider`."""
    return ContractsProvider(value.value if isinstance(value, DbProvider) else str(value))


def _contracts_state(value: object) -> ContractsSyncState:
    """DB ``PMSyncState`` (or raw string) -> the contracts :class:`PMSyncState`."""
    return ContractsSyncState(value.value if isinstance(value, DbSyncState) else str(value))


class DbLinkRepository:
    """A Postgres-backed link repository (implements ``LinkRepository``)."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory

    # ------------------------------------------------------------------ #
    # Mapping                                                             #
    # ------------------------------------------------------------------ #

    def _apply(self, row: PMTaskLink, link: LinkRecord) -> None:
        """Write every domain field onto ``row`` (used by ``upsert``)."""
        row.workspace_id = link.workspace_id
        row.connection_id = link.connection_id
        row.forge_task_id = link.forge_task_id
        row.provider = _db_provider(link.provider)
        row.external_id = link.external_id
        row.external_key = link.external_key
        row.external_url = link.external_url
        row.last_synced_at = link.last_synced_at
        row.forge_version_at_sync = link.forge_version_at_sync
        row.external_updated_at_at_sync = link.external_updated_at_at_sync
        row.last_outbound_hash = link.last_outbound_hash
        row.last_inbound_hash = link.last_inbound_hash
        row.sync_state = _db_state(link.sync_state)
        row.conflict_detail = (
            dict(link.conflict_detail) if link.conflict_detail is not None else None
        )
        row.last_error = link.last_error

    def _to_domain(self, row: PMTaskLink) -> LinkRecord:
        """Rebuild a fresh :class:`LinkRecord` from a persisted row."""
        return LinkRecord(
            id=row.id,
            connection_id=row.connection_id,
            workspace_id=row.workspace_id,
            forge_task_id=row.forge_task_id,
            provider=_contracts_provider(row.provider),
            external_id=row.external_id,
            external_key=row.external_key,
            external_url=row.external_url,
            last_synced_at=_aware(row.last_synced_at),
            forge_version_at_sync=row.forge_version_at_sync,
            external_updated_at_at_sync=_aware(row.external_updated_at_at_sync),
            last_outbound_hash=row.last_outbound_hash,
            last_inbound_hash=row.last_inbound_hash,
            sync_state=_contracts_state(row.sync_state),
            conflict_detail=(
                dict(row.conflict_detail) if row.conflict_detail is not None else None
            ),
            last_error=row.last_error,
        )

    # ------------------------------------------------------------------ #
    # LinkRepository protocol                                             #
    # ------------------------------------------------------------------ #

    def get(self, link_id: UUID) -> LinkRecord | None:
        with self._sf() as session:
            row = session.get(PMTaskLink, link_id)
            return self._to_domain(row) if row is not None else None

    def get_by_forge_task(
        self, connection_id: UUID, forge_task_id: UUID
    ) -> LinkRecord | None:
        with self._sf() as session:
            row = session.scalars(
                select(PMTaskLink)
                .where(
                    PMTaskLink.connection_id == connection_id,
                    PMTaskLink.forge_task_id == forge_task_id,
                )
                .limit(1)
            ).first()
            return self._to_domain(row) if row is not None else None

    def get_by_external(
        self, connection_id: UUID, external_id: str
    ) -> LinkRecord | None:
        with self._sf() as session:
            row = session.scalars(
                select(PMTaskLink)
                .where(
                    PMTaskLink.connection_id == connection_id,
                    PMTaskLink.external_id == external_id,
                )
                .limit(1)
            ).first()
            return self._to_domain(row) if row is not None else None

    def upsert(self, link: LinkRecord) -> LinkRecord:
        with self._sf() as session:
            row = session.get(PMTaskLink, link.id)
            if row is None:
                row = PMTaskLink(id=link.id)
                self._apply(row, link)
                session.add(row)
            else:
                self._apply(row, link)
            session.commit()
        return link.model_copy(deep=True)

    def delete(self, link_id: UUID) -> None:
        with self._sf() as session:
            row = session.get(PMTaskLink, link_id)
            if row is not None:
                session.delete(row)
                session.commit()

    def list_by_state(
        self, connection_id: UUID, state: ContractsSyncState
    ) -> list[LinkRecord]:
        with self._sf() as session:
            rows = session.scalars(
                select(PMTaskLink)
                .where(
                    PMTaskLink.connection_id == connection_id,
                    PMTaskLink.sync_state == _db_state(state),
                )
                .order_by(PMTaskLink.created_at.asc(), PMTaskLink.id.asc())
            ).all()
            return [self._to_domain(r) for r in rows]
