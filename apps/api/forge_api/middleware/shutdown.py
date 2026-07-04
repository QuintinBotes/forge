"""Graceful shutdown + readiness state (HARD-11).

FastAPI ``create_app`` gains a ``lifespan`` that, on SIGTERM / process shutdown,
flips the readiness flag so a load balancer stops routing new traffic, drains
in-flight requests for a bounded grace period, then disposes the DB engine,
closes the Redis pool, and flushes the telemetry exporters — so a rolling
restart never drops a request or loses an audit row.

``/health`` (liveness) stays ``200`` while the process is up; the new
``/health/ready`` (readiness) returns ``503`` the instant draining begins.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI
    from starlette.types import ASGIApp, Receive, Scope, Send

__all__ = [
    "InFlightMiddleware",
    "ShutdownState",
    "current_state",
    "dispose_db_engine",
    "flush_exporters",
    "lifespan",
    "set_current_state",
    "shutdown_close_redis",
]


class ShutdownState:
    """Process serving/draining flag plus an in-flight request counter.

    ``is_serving`` starts ``True`` so readiness is healthy even when the app is
    built without a lifespan (many unit tests). ``begin_drain`` flips it to
    ``False`` (readiness → 503); the lifespan then waits for ``in_flight`` to
    reach zero (bounded by the drain grace) before tearing resources down.
    """

    def __init__(self, *, serving: bool = True) -> None:
        self._serving = serving
        self._in_flight = 0
        self._lock = threading.Lock()

    @property
    def is_serving(self) -> bool:
        return self._serving

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    def mark_serving(self) -> None:
        self._serving = True

    def begin_drain(self) -> None:
        """Stop advertising readiness; new traffic should be drained away."""
        self._serving = False

    def enter_request(self) -> None:
        with self._lock:
            self._in_flight += 1

    def exit_request(self) -> None:
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)


class InFlightMiddleware:
    """ASGI middleware counting in-flight HTTP requests for the drain wait."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        state = current_state()
        state.enter_request()
        try:
            await self.app(scope, receive, send)
        finally:
            state.exit_request()


_CURRENT: ShutdownState | None = None
_CURRENT_LOCK = threading.Lock()


def current_state() -> ShutdownState:
    """Return the process-wide shutdown state (created serving by default)."""
    global _CURRENT
    with _CURRENT_LOCK:
        if _CURRENT is None:
            _CURRENT = ShutdownState()
        return _CURRENT


def set_current_state(state: ShutdownState | None) -> None:
    """Replace the process-wide shutdown state (lifespan / test seam)."""
    global _CURRENT
    with _CURRENT_LOCK:
        _CURRENT = state


# --------------------------------------------------------------------------- #
# Resource teardown hooks (module-level so tests can spy/monkeypatch them).    #
# --------------------------------------------------------------------------- #


def dispose_db_engine() -> None:
    """Dispose the process-wide SQLAlchemy engine if one was ever built.

    Uses the ``lru_cache`` on :func:`forge_api.db.get_engine` to avoid *creating*
    an engine just to dispose it — only an already-materialized pool is closed.
    """
    from forge_api.db import get_engine

    if get_engine.cache_info().currsize:  # engine was built at least once
        get_engine().dispose()


def shutdown_close_redis(redis_client: Any | None) -> None:
    """Close a Redis client/pool if one is held (best-effort)."""
    if redis_client is None:
        return
    close = getattr(redis_client, "close", None)
    if callable(close):
        with contextlib.suppress(Exception):
            close()


def flush_exporters() -> None:
    """Flush OTel/audit exporters so no telemetry is lost on restart."""
    with contextlib.suppress(Exception):
        from forge_obs.telemetry import flush_telemetry  # type: ignore[attr-defined]

        flush_telemetry()


async def _drain(state: ShutdownState, *, timeout_s: float) -> None:
    """Wait up to ``timeout_s`` for in-flight requests to complete."""
    deadline = asyncio.get_event_loop().time() + max(0.0, timeout_s)
    while state.in_flight > 0 and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.05)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup: mark serving. Shutdown: drain, then tear resources down.

    On shutdown the sequence is: flip readiness to 503 (``begin_drain``), wait up
    to ``FORGE_SHUTDOWN_DRAIN_SECONDS`` for in-flight work, dispose the DB engine,
    close the Redis pool, flush the telemetry exporters.
    """
    from forge_api.settings import get_settings

    state = ShutdownState(serving=True)
    set_current_state(state)
    app.state.shutdown_state = state
    try:
        yield
    finally:
        state.begin_drain()
        drain_seconds = float(getattr(get_settings(), "shutdown_drain_seconds", 30))
        await _drain(state, timeout_s=drain_seconds)
        dispose_db_engine()
        shutdown_close_redis(getattr(app.state, "redis", None))
        flush_exporters()
        # Clear the process-wide reference once teardown completes. In production
        # the ASGI server has stopped serving by now; in a reused test process
        # this prevents a finished app's drained state from making the next
        # (lifespan-less) app's readiness probe report 503.
        set_current_state(None)
