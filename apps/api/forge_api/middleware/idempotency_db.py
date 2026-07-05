"""Postgres-backed HTTP idempotency store (idempotency-store persistence).

:class:`DbIdempotencyStore` is a drop-in, durable alternative to
:class:`~forge_api.middleware.idempotency.InMemoryIdempotencyStore` that satisfies
the **same** :class:`~forge_api.middleware.idempotency.IdempotencyStore` protocol
(``get`` / ``put_if_absent``) that :class:`~forge_api.middleware.idempotency.IdempotencyMiddleware`
reserves and replays through. The composition root swaps it in behind
``FORGE_IDEMPOTENCY_BACKEND=db``; the default stays ``memory`` and the in-memory
store remains the unit-test default, so no existing behaviour changes.

Behaviour parity with the in-memory store is exact and *intentional*:

* **TTL / expiry semantics.** The in-memory store evicts on a monotonic clock;
  this uses a persisted wall-clock ``expires_at`` (``now + max(1, ttl_s)``). A
  read past ``expires_at`` returns ``None`` (the entry is *absent*), and a reserve
  against an expired key overwrites it and returns ``True`` — mirroring the
  in-memory ``_evict``-then-check exactly.
* **Concurrent reserve is atomic.** ``put_if_absent`` is a single
  ``INSERT ... ON CONFLICT (key) DO UPDATE ... WHERE expires_at <= now``: a fresh
  key inserts, an *expired* key is overwritten, and a *live* key is left untouched.
  ``RETURNING`` tells the caller which happened — a row means we wrote (``True``),
  no row means a live entry already existed (``False``). Two racing first-sights of
  the same key thus collapse to exactly one winner at the database, so the guarded
  side effect is cached once even across processes.
* **Byte-exact response round-trip.** The ``StoredResponse`` is stored as its JSONB
  image with the ``body`` base64-encoded (JSON holds no raw bytes); ``get`` decodes
  it back verbatim, and ``created_at`` is persisted from the record (not the DB
  clock), so a replayed response equals the original the middleware captured.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from forge_api.middleware.idempotency import (
    IdempotencyStore,
    InMemoryIdempotencyStore,
    StoredResponse,
)
from forge_db.models import IdempotencyKey

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from forge_api.settings import Settings

__all__ = ["DbIdempotencyStore", "build_idempotency_store"]


def _aware(value: datetime) -> datetime:
    """Normalise a stored timestamp to timezone-aware UTC (defensive).

    A ``timestamptz`` reads back aware; a naive value (e.g. a SQLite round-trip)
    is assumed UTC so the expiry comparison and the replayed ``created_at`` stay
    correct on every dialect.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _dump(value: StoredResponse) -> dict[str, Any]:
    """The JSONB image of a ``StoredResponse`` (body base64-encoded)."""
    return {
        "request_hash": value.request_hash,
        "status_code": value.status_code,
        "content_type": value.content_type,
        "body_b64": base64.b64encode(value.body).decode("ascii"),
    }


def _load(payload: dict[str, Any], created_at: datetime) -> StoredResponse:
    """Rebuild the exact ``StoredResponse`` that produced ``payload``."""
    return StoredResponse(
        request_hash=payload["request_hash"],
        status_code=payload["status_code"],
        content_type=payload.get("content_type", "application/json"),
        body=base64.b64decode(payload["body_b64"]),
        created_at=_aware(created_at),
    )


class DbIdempotencyStore:
    """A Postgres-backed idempotency store (implements ``IdempotencyStore``)."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory

    def get(self, key: str) -> StoredResponse | None:
        """The cached response for ``key`` if present *and* unexpired, else ``None``."""
        now = datetime.now(UTC)
        with self._sf() as session:
            row = session.execute(
                select(IdempotencyKey).where(IdempotencyKey.key == key)
            ).scalar_one_or_none()
            if row is None or _aware(row.expires_at) <= now:
                return None
            return _load(row.response, row.created_at)

    def put_if_absent(self, key: str, value: StoredResponse, ttl_s: int) -> bool:
        """Reserve ``key`` iff no *live* entry exists; return ``True`` when it wrote.

        Atomic get-or-set: a fresh key inserts, an expired key is overwritten, a
        live key is a no-op. ``RETURNING`` distinguishes the write from the no-op.
        """
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=max(1, ttl_s))
        payload = _dump(value)
        stmt = (
            pg_insert(IdempotencyKey)
            .values(
                key=key,
                response=payload,
                created_at=value.created_at,
                expires_at=expires_at,
            )
            .on_conflict_do_update(
                index_elements=["key"],
                set_={
                    "response": payload,
                    "created_at": value.created_at,
                    "expires_at": expires_at,
                },
                where=IdempotencyKey.expires_at <= now,
            )
            .returning(IdempotencyKey.key)
        )
        with self._sf() as session:
            wrote = session.execute(stmt).scalar_one_or_none() is not None
            session.commit()
            return wrote

    def purge_expired(self) -> int:
        """Delete every entry past its TTL; return the count (sweep helper).

        Not part of the ``IdempotencyStore`` protocol — an operator-facing hygiene
        path (the in-memory store evicts lazily on access; a durable table needs an
        explicit sweep). ``get`` / ``put_if_absent`` already treat an expired row as
        absent, so this only reclaims space.
        """
        now = datetime.now(UTC)
        with self._sf() as session:
            result = session.execute(delete(IdempotencyKey).where(IdempotencyKey.expires_at <= now))
            session.commit()
            return int(result.rowcount or 0)


# --------------------------------------------------------------------------- #
# Composition root                                                             #
# --------------------------------------------------------------------------- #


def build_idempotency_store(settings: Settings | None = None) -> IdempotencyStore:
    """Return the idempotency store selected by ``FORGE_IDEMPOTENCY_BACKEND``.

    ``memory`` (default) → the hermetic :class:`InMemoryIdempotencyStore` (unit-test
    default, no Postgres); ``db`` → the durable :class:`DbIdempotencyStore` bound to
    the shared session factory. Both satisfy the same frozen ``IdempotencyStore``
    protocol, so the middleware behaves identically on either.
    """
    from forge_api.settings import get_settings

    settings = settings or get_settings()
    if settings.idempotency_backend == "db":
        from forge_api.db import get_session_factory

        return DbIdempotencyStore(get_session_factory())
    return InMemoryIdempotencyStore()
