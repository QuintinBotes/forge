"""Durable backing table for the HTTP idempotency-key response cache (idempotency-store persist).

The API's :class:`forge_api.middleware.idempotency.InMemoryIdempotencyStore` is
the store behind :class:`~forge_api.middleware.idempotency.IdempotencyMiddleware`:
a tenant-scoped ``key`` → :class:`~forge_api.middleware.idempotency.StoredResponse`
map with a per-entry TTL, so a retried unsafe request carrying the same
``Idempotency-Key`` replays the first response instead of re-running the side
effect. This module is the Postgres backing for the *db* variant of that store
(:class:`forge_api.middleware.idempotency_db.DbIdempotencyStore`): one row per
cached response.

Why a **new** table rather than reusing a per-domain ``idempotency_key`` column:
the two are unrelated concerns. ``automation_execution.idempotency_key`` and
``deployment.idempotency_key`` dedupe a *specific domain command* at the service
layer; this is the *transport-level* HTTP response cache the middleware owns,
holding an opaque cached response for any unsafe route. There is no general HTTP
idempotency store table, so this ``idempotency_key`` table is it.

Storage-boundary fidelity (the middleware must round-trip a ``StoredResponse``
exactly, or a replay would differ from the original):

* ``key`` is the middleware's fully-qualified, tenant-scoped cache key
  (``forge:idem:<tenant>:<header>``); it is a UNIQUE column (not the PK) because
  the foundation mandates a surrogate UUID ``id`` PK + timestamps on every table
  (same deviation :class:`~forge_db.models.sso.SamlReplay` documents). The UNIQUE
  index is the ``ON CONFLICT`` target the reserve/get-or-set path serializes on.
* ``response`` is the JSONB image of the ``StoredResponse`` value object:
  ``request_hash`` (the middleware's 422-on-mismatch guard), ``status_code``,
  ``content_type``, and the response ``body`` as a base64 ``body_b64`` string
  (JSON cannot hold raw bytes, so the body is base64-encoded and decoded verbatim
  on read — a byte-exact round-trip).
* ``created_at`` (inherited, timezone-aware) is persisted from the record's own
  ``StoredResponse.created_at`` rather than the DB clock, so a round-tripped entry
  equals the one the middleware stored.
* ``expires_at`` (timezone-aware) is the wall-clock TTL horizon (``created_at`` /
  reserve-time ``+ ttl``); a read past it is treated as absent, and the
  reserve path overwrites it — the durable analogue of the in-memory store's
  monotonic-clock eviction. Indexed so an expiry sweep touches only stale rows.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from forge_db.base import ForgeModel, json_type


class IdempotencyKey(ForgeModel):
    """One cached HTTP response keyed by a tenant-scoped idempotency token.

    Not workspace-scoped: the tenant is already baked into ``key`` (the middleware
    hashes the presented credential into it), and an anonymous request scopes by
    client IP — there is no ``workspace`` FK to hang it on, so this uses the plain
    :class:`ForgeModel` (surrogate UUID PK + timestamps) like
    :class:`~forge_db.models.sso.SamlReplay`.
    """

    __tablename__ = "idempotency_key"
    __table_args__ = (
        # Expiry sweep / read-time-expiry queries touch only rows near their TTL.
        Index("ix_idempotency_key_expires_at", "expires_at"),
    )

    #: The middleware's fully-qualified, tenant-scoped cache key. UNIQUE (not PK)
    #: per the house UUID-PK invariant; it is the ``ON CONFLICT`` target.
    key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    #: JSONB image of the ``StoredResponse`` (``request_hash`` / ``status_code`` /
    #: ``content_type`` / base64 ``body_b64``); the body is opaque bytes.
    response: Mapped[dict[str, Any]] = mapped_column(json_type(), nullable=False)
    #: Wall-clock TTL horizon; a read past it is absent, the reserve path overwrites.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"IdempotencyKey(id={self.id!r}, key={self.key!r}, expires_at={self.expires_at!r})"


__all__ = ["IdempotencyKey"]
