"""Realtime event publisher + fan-out (slice RT-2).

Write paths across the API (board, incidents, approvals) emit a
:class:`~forge_contracts.RealtimeEvent`; a :class:`~forge_contracts.Broadcaster`
fans it out to the live ``/ws`` sockets. Two implementations sit behind the
frozen ``Broadcaster`` protocol:

* :class:`InProcessBroadcaster` — the default. ``publish`` forwards the event
  straight to the process-wide :class:`ConnectionManager`, so a single-worker
  deployment needs no external infrastructure. It also feeds any in-process
  topic subscribers (the seam the spec-collab channel builds on).
* :class:`RedisBroadcaster` — selected when ``settings.realtime_backend ==
  "redis"`` (connecting to ``settings.redis_url``). ``publish`` writes the event
  to a Redis pub/sub channel; a per-worker subscriber loop (:meth:`run_forever`,
  started in the app lifespan) receives every worker's events — including its
  own — and delivers them to that worker's :class:`ConnectionManager`. A
  broadcast on one API worker therefore reaches sockets pinned to another.

Fan-out is best-effort: :func:`emit_event` swallows any broadcaster failure so a
dead socket or a Redis hiccup can never fail the primary write. Tenant isolation
is preserved end-to-end — delivery is always keyed on ``event.workspace_id`` via
the :class:`ConnectionManager`, which only ever addresses that workspace's room.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, Request

from forge_api.realtime.manager import ConnectionManager, get_connection_manager
from forge_contracts import Broadcaster, RealtimeEvent

if TYPE_CHECKING:
    from fastapi import FastAPI

    from forge_api.settings import Settings

#: Redis pub/sub channel prefix. The topic (a workspace id) is appended so a
#: pattern subscribe (``forge:rt:*``) sees every workspace's stream on a worker.
_CHANNEL_PREFIX = "forge:rt:"


def _envelope(event: RealtimeEvent) -> dict[str, Any]:
    """Serialize an event to the wire shape the web board hook consumes.

    The hook branches on ``type`` and reads the optional cross-reference ids
    (``task_id`` etc.); ``workspace_id`` is an internal routing field the client
    never needs, and ``None`` ids are dropped so the envelope stays ``{"type":
    ..., "task_id": ...}`` — byte-identical to what slice RT-1 broadcasts.
    """
    return event.model_dump(mode="json", exclude_none=True, exclude={"workspace_id"})


class InProcessBroadcaster:
    """Default broadcaster: forward events to the in-process connection registry.

    Stateless over the shared :class:`ConnectionManager`, so a fresh instance per
    request is cheap. ``subscribe`` backs an in-process topic pub/sub (used by
    later spec-collab work); the board push path only needs ``publish``.
    """

    def __init__(self, manager: ConnectionManager) -> None:
        self._manager = manager
        self._subscribers: dict[str, set[asyncio.Queue[RealtimeEvent]]] = {}

    async def publish(self, topic: str, event: RealtimeEvent) -> None:
        """Deliver ``event`` to the workspace's sockets and any topic subscribers."""
        await self._manager.broadcast(event.workspace_id, _envelope(event))
        for queue in list(self._subscribers.get(topic, ())):
            queue.put_nowait(event)

    async def subscribe(self, topic: str) -> AsyncIterator[RealtimeEvent]:
        """Yield events published to ``topic`` until the consumer stops iterating."""
        queue: asyncio.Queue[RealtimeEvent] = asyncio.Queue()
        self._subscribers.setdefault(topic, set()).add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            subscribers = self._subscribers.get(topic)
            if subscribers is not None:
                subscribers.discard(queue)
                if not subscribers:
                    self._subscribers.pop(topic, None)


class RedisBroadcaster:
    """Redis pub/sub broadcaster for multi-worker deployments.

    ``publish`` writes to a per-workspace channel; :meth:`run_forever` — one task
    per worker, started in the app lifespan — pattern-subscribes to every channel
    and replays each event into that worker's :class:`ConnectionManager`. The
    publishing worker receives its own message back through the same loop, so
    there is a single, uniform delivery path (no local/remote de-duplication).
    """

    def __init__(self, client: Any, manager: ConnectionManager) -> None:
        self._redis = client
        self._manager = manager

    async def publish(self, topic: str, event: RealtimeEvent) -> None:
        await self._redis.publish(f"{_CHANNEL_PREFIX}{topic}", event.model_dump_json())

    async def subscribe(self, topic: str) -> AsyncIterator[RealtimeEvent]:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(f"{_CHANNEL_PREFIX}{topic}")
        try:
            async for message in pubsub.listen():
                if message.get("type") == "message":
                    yield RealtimeEvent.model_validate_json(message["data"])
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(f"{_CHANNEL_PREFIX}{topic}")
                await pubsub.aclose()

    async def run_forever(self) -> None:
        """Deliver every worker's published events to this worker's sockets.

        Runs for the lifetime of the process (cancelled on shutdown). Delivery is
        keyed on the decoded ``event.workspace_id``, so isolation holds regardless
        of which channel carried the message.
        """
        pubsub = self._redis.pubsub()
        await pubsub.psubscribe(f"{_CHANNEL_PREFIX}*")
        try:
            async for message in pubsub.listen():
                if message.get("type") != "pmessage":
                    continue
                event = RealtimeEvent.model_validate_json(message["data"])
                await self._manager.broadcast(event.workspace_id, _envelope(event))
        finally:
            with contextlib.suppress(Exception):
                await pubsub.aclose()


async def emit_event(broadcaster: Broadcaster, event: RealtimeEvent) -> None:
    """Publish ``event`` best-effort — a fan-out failure never fails the write.

    The primary mutation has already committed by the time a write path calls
    this; realtime delivery is a courtesy on top, so any broadcaster error
    (broken peer, Redis unavailable) is swallowed rather than surfaced as a 500.
    """
    with contextlib.suppress(Exception):
        await broadcaster.publish(str(event.workspace_id), event)


def get_broadcaster(
    request: Request,
    manager: Annotated[ConnectionManager, Depends(get_connection_manager)],
) -> Broadcaster:
    """Return the active broadcaster (test/override seam).

    The app lifespan installs a :class:`RedisBroadcaster` on ``app.state`` when
    the Redis backend is selected; otherwise a stateless
    :class:`InProcessBroadcaster` over the DI-resolved connection manager is
    returned (overridable in tests by overriding ``get_connection_manager`` or
    this dependency directly).
    """
    existing = getattr(request.app.state, "broadcaster", None)
    if existing is not None:
        return existing
    return InProcessBroadcaster(manager)


def realtime_redis_enabled(settings: Settings) -> bool:
    """Whether the Redis fan-out backend is selected (``FORGE_REALTIME_BACKEND=redis``)."""
    return settings.realtime_backend == "redis"


async def startup_realtime(app: FastAPI, settings: Settings) -> None:
    """Lifespan startup hook: wire the Redis broadcaster when it is selected.

    A no-op for the default in-process backend, so a dev/test instance boots
    without any Redis dependency. Best-effort: a misconfigured/unavailable Redis
    parks the fan-out (the subscriber loop reports the failure) rather than
    aborting startup.
    """
    if not realtime_redis_enabled(settings):
        return
    from redis.asyncio import Redis

    client = Redis.from_url(settings.redis_url)
    broadcaster = RedisBroadcaster(client, get_connection_manager())
    app.state.redis = client
    app.state.broadcaster = broadcaster
    app.state.realtime_subscriber = asyncio.create_task(broadcaster.run_forever())


async def shutdown_realtime(app: FastAPI) -> None:
    """Lifespan shutdown hook: cancel the subscriber loop and close the Redis client.

    Clears ``app.state.redis`` after closing so the generic ``shutdown_close_redis``
    teardown does not double-close the (async) client.
    """
    task = getattr(app.state, "realtime_subscriber", None)
    if task is not None:
        task.cancel()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await task
        app.state.realtime_subscriber = None
    client = getattr(app.state, "redis", None)
    if client is not None:
        with contextlib.suppress(Exception):
            await client.aclose()
        app.state.redis = None
    app.state.broadcaster = None


__all__ = [
    "Broadcaster",
    "InProcessBroadcaster",
    "RedisBroadcaster",
    "emit_event",
    "get_broadcaster",
    "realtime_redis_enabled",
    "shutdown_realtime",
    "startup_realtime",
]
