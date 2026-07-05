"""Postgres-backed observability audit store (audit-store persistence).

:class:`DbAuditStore` is a drop-in, durable alternative to
:class:`~forge_api.observability.audit.InMemoryAuditStore` that satisfies the
**same** :class:`~forge_api.observability.audit.AuditStore` protocol
(``append`` / ``all`` / ``query`` / ``verify_integrity``) — so the composition
root swaps it in behind ``FORGE_AUDIT_BACKEND=db`` with no behavioural change.
The default stays ``memory`` and the in-memory store remains the unit-test
default; this is the sink the MCP db-path
(``FORGE_MCP_AUDIT_BACKEND=db`` → ``MCPAuditSink(AuditLog())``) forwards to, so
with both flags set every live MCP call finally lands in durable Postgres.

Behaviour parity is exact and *intentional*:

* the global, 0-based, monotonic ``seq`` (spanning every workspace) is preserved
  by a single ``observability_audit_chain_head`` cursor row locked ``FOR UPDATE``
  on append — the DB analogue of the in-memory ``len(entries)`` scheme;
* the tamper-evident hash chain re-uses the store's own
  :func:`~forge_api.observability.audit._hash_entry` /
  :func:`~forge_api.observability.audit.verify_chain`, so a round-tripped row
  re-hashes identically and ``verify_integrity`` walks the same chain;
* ``query`` reproduces the in-memory filter/limit semantics byte-for-byte
  (including the "``limit`` is the *most recent* N, ``limit==0`` → empty,
  negative ``limit`` → ignored" edge cases).

The one fidelity detail is the entry ``timestamp``: it is normalised to
timezone-aware UTC on the way *in* (before hashing + persisting) and again on the
way *out*, so the ``timestamptz`` round-trip reproduces the exact datetime that
was hashed on every dialect — the chain never breaks from an offset/representation
drift. Because the chain lives in Postgres, independently constructed
``DbAuditStore`` instances (e.g. the observability service and the MCP sink) all
converge on one durable, shared trail.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from forge_api.observability.audit import (
    GENESIS_HASH,
    AuditCategory,
    AuditEntry,
    AuditLog,
    AuditStore,
    InMemoryAuditStore,
    _hash_entry,
    verify_chain,
)
from forge_db.models.observability_audit import (
    CHAIN_HEAD_ID,
    ObservabilityAuditChainHead,
    ObservabilityAuditEntry,
)

__all__ = ["DbAuditStore", "build_audit_store", "default_audit_log"]


def _to_utc(value: datetime) -> datetime:
    """Normalise a datetime to timezone-aware UTC (stable across store + DB).

    Applied identically before hashing/persisting and after reading back, so the
    ``timestamptz`` round-trip reproduces the exact instant that was hashed — a
    naive value is assumed UTC; an aware value is converted.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class DbAuditStore:
    """A Postgres-backed, append-only audit store (implements ``AuditStore``)."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory

    # ------------------------------------------------------------------ #
    # Mapping                                                             #
    # ------------------------------------------------------------------ #

    def _to_entry(self, row: ObservabilityAuditEntry) -> AuditEntry:
        """Rebuild the exact :class:`AuditEntry` that produced ``row`` (re-hashable)."""
        return AuditEntry(
            seq=row.seq,
            id=row.entry_id,
            timestamp=_to_utc(row.occurred_at),
            category=AuditCategory(row.category),
            action=row.action,
            actor=row.actor,
            workspace_id=row.workspace_ref,
            run_id=row.run_id,
            target=row.target,
            connection_id=row.connection_id,
            status=row.status,
            detail=row.detail,
            payload_hash=row.payload_hash,
            latency_ms=row.latency_ms,
            metadata=dict(row.entry_metadata or {}),
            redacted=row.redacted,
            prev_hash=row.prev_hash,
            entry_hash=row.entry_hash,
        )

    # ------------------------------------------------------------------ #
    # AuditStore protocol                                                 #
    # ------------------------------------------------------------------ #

    def append(self, entry: AuditEntry) -> AuditEntry:
        """Stamp ``entry`` into the global chain and persist it durably."""
        # Canonicalise the timestamp up front so the hash we compute here matches
        # the one recomputed after a timestamptz round-trip.
        canonical = entry.model_copy(update={"timestamp": _to_utc(entry.timestamp)})
        with self._sf() as session:
            head = session.execute(
                select(ObservabilityAuditChainHead)
                .where(ObservabilityAuditChainHead.id == CHAIN_HEAD_ID)
                .with_for_update()
            ).scalar_one_or_none()
            if head is None:
                head = ObservabilityAuditChainHead(
                    id=CHAIN_HEAD_ID, last_seq=-1, last_hash=GENESIS_HASH
                )
                session.add(head)
                session.flush()

            seq = head.last_seq + 1
            prev = head.last_hash
            stamped = canonical.model_copy(
                update={"seq": seq, "prev_hash": prev, "entry_hash": None}
            )
            entry_hash = _hash_entry(stamped)
            final = stamped.model_copy(update={"entry_hash": entry_hash})

            session.add(
                ObservabilityAuditEntry(
                    entry_id=final.id,
                    seq=seq,
                    occurred_at=final.timestamp,
                    category=final.category.value,
                    action=final.action,
                    actor=final.actor,
                    workspace_ref=final.workspace_id,
                    run_id=final.run_id,
                    target=final.target,
                    connection_id=final.connection_id,
                    status=final.status,
                    detail=final.detail,
                    payload_hash=final.payload_hash,
                    latency_ms=final.latency_ms,
                    entry_metadata=dict(final.metadata),
                    redacted=final.redacted,
                    prev_hash=prev,
                    entry_hash=entry_hash,
                )
            )
            head.last_seq = seq
            head.last_hash = entry_hash
            session.commit()
            return final

    def all(self) -> list[AuditEntry]:
        """Every entry, oldest-first (chain order)."""
        with self._sf() as session:
            rows = (
                session.execute(
                    select(ObservabilityAuditEntry).order_by(ObservabilityAuditEntry.seq.asc())
                )
                .scalars()
                .all()
            )
            return [self._to_entry(r) for r in rows]

    def query(
        self,
        *,
        category: AuditCategory | None = None,
        actor: str | None = None,
        run_id: uuid.UUID | None = None,
        connection_id: str | None = None,
        workspace_id: uuid.UUID | None = None,
        limit: int | None = None,
    ) -> list[AuditEntry]:
        """Filtered, chain-ordered entries (semantics identical to the in-memory store)."""
        stmt = select(ObservabilityAuditEntry)
        if category is not None:
            stmt = stmt.where(ObservabilityAuditEntry.category == category.value)
        if actor is not None:
            stmt = stmt.where(ObservabilityAuditEntry.actor == actor)
        if run_id is not None:
            stmt = stmt.where(ObservabilityAuditEntry.run_id == run_id)
        if connection_id is not None:
            stmt = stmt.where(ObservabilityAuditEntry.connection_id == connection_id)
        if workspace_id is not None:
            stmt = stmt.where(ObservabilityAuditEntry.workspace_ref == workspace_id)

        with self._sf() as session:
            # ``limit`` mirrors ``rows[-limit:] if limit else []`` for limit>=0:
            # the *most recent* N in chain order; 0 → empty; None/negative → all.
            if limit is not None and limit >= 0:
                if limit == 0:
                    return []
                rows = list(
                    session.execute(stmt.order_by(ObservabilityAuditEntry.seq.desc()).limit(limit))
                    .scalars()
                    .all()
                )
                rows.reverse()
                return [self._to_entry(r) for r in rows]

            rows = session.execute(stmt.order_by(ObservabilityAuditEntry.seq.asc())).scalars().all()
            return [self._to_entry(r) for r in rows]

    def verify_integrity(self) -> bool:
        """Re-walk the persisted global chain (same verifier as the in-memory store)."""
        return verify_chain(self.all())

    def count(self) -> int:
        """Number of persisted entries (helper; not part of the protocol).

        Deliberately *not* ``__len__``: an empty store must stay truthy so
        ``AuditLog(store)`` never mistakes it for "no store" and falls back to an
        in-memory one.
        """
        with self._sf() as session:
            return int(
                session.execute(
                    select(func.count()).select_from(ObservabilityAuditEntry)
                ).scalar_one()
            )


# --------------------------------------------------------------------------- #
# Composition root                                                             #
# --------------------------------------------------------------------------- #


def build_audit_store() -> AuditStore:
    """Return the process-wide audit store selected by ``FORGE_AUDIT_BACKEND``.

    ``memory`` (default) → the hermetic :class:`InMemoryAuditStore` (unit-test
    default, no Postgres); ``db`` → the durable :class:`DbAuditStore` bound to the
    shared session factory. Both satisfy the same frozen ``AuditStore`` protocol.
    """
    from forge_api.settings import get_settings

    if get_settings().audit_backend == "db":
        from forge_api.db import get_session_factory

        return DbAuditStore(get_session_factory())
    return InMemoryAuditStore()


def default_audit_log() -> AuditLog:
    """Return an :class:`AuditLog` over the env-selected store (see ``build_audit_store``)."""
    return AuditLog(build_audit_store())
