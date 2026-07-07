"""HARD-11 reliability middleware for the Forge API.

Three composable, default-on primitives a self-hosted operator needs to run
Forge without losing or duplicating work:

* :mod:`forge_api.middleware.shutdown` — ``ShutdownState`` + ``lifespan`` +
  ``InFlightMiddleware`` for zero-downtime graceful shutdown / readiness.
* :mod:`forge_api.middleware.idempotency` — ``IdempotencyMiddleware`` collapsing
  retries of an ``Idempotency-Key`` request so the side effect runs once.

Rate limiting itself lives in :mod:`forge_api.security.ratelimit` (shipped by
HARD-09 and extended here with per-route overrides + ``X-RateLimit-*`` headers);
:func:`install_middleware` wires the reliability layer on top of it in the
required outside-in order.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from forge_api.middleware.idempotency import (
    IdempotencyMiddleware,
    IdempotencyStore,
    InMemoryIdempotencyStore,
    StoredResponse,
)
from forge_api.middleware.shutdown import (
    InFlightMiddleware,
    ShutdownState,
    current_state,
    lifespan,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from forge_api.settings import Settings

__all__ = [
    "IdempotencyMiddleware",
    "IdempotencyStore",
    "InFlightMiddleware",
    "InMemoryIdempotencyStore",
    "ShutdownState",
    "StoredResponse",
    "current_state",
    "install_middleware",
    "lifespan",
]


def install_middleware(app: FastAPI, settings: Settings) -> None:
    """Install the reliability middleware layer, outside-in.

    ``add_middleware`` wraps outermost-last, so adding in-flight tracking →
    idempotency yields the runtime order: idempotency outermost (a replay
    short-circuits before anything else), then in-flight counting, then the
    existing HARD-09 security stack (rate limit / body limit / headers) mounted
    by ``create_app``. Each layer is default-on but no-ops when disabled or when
    its trigger (an ``Idempotency-Key`` header) is absent.
    """
    # In-flight counter so the lifespan drain waits for real requests to finish.
    app.add_middleware(InFlightMiddleware)

    if settings.idempotency_enabled:
        from forge_api.middleware.idempotency_db import build_idempotency_store

        app.add_middleware(
            IdempotencyMiddleware,
            store=build_idempotency_store(settings),
            ttl_s=settings.idempotency_ttl_seconds,
            enabled=True,
        )
