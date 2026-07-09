"""Per-workspace WebSocket connection registry (slice RT-1).

The :class:`ConnectionManager` holds the live board-push sockets grouped into
one *room* per ``workspace_id`` — the mandatory tenant-isolation boundary every
Forge surface enforces. A broadcast for a workspace only ever reaches sockets
registered under that same workspace id; a foreign workspace's clients never
observe the event.

The manager is deliberately transport-only: it does not authenticate (the
router does that before :meth:`connect`) and it does not know the shape of the
events it relays — it forwards the JSON envelope the web board hook expects
(``{"type": ..., "task_id": ...}``) verbatim. A later slice layers the optional
Redis pub/sub fan-out on top of this same in-process registry so a broadcast on
one API worker reaches sockets pinned to another.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from functools import lru_cache
from typing import Any
from uuid import UUID

from fastapi import WebSocket


class ConnectionManager:
    """Track live board-push WebSockets, grouped into one room per workspace.

    Concurrency: FastAPI/Starlette runs every connection handler on the same
    event loop, so mutation of the room map is single-threaded. The
    :class:`asyncio.Lock` guards the *await* points inside :meth:`broadcast`
    (a slow ``send_json`` must not let a concurrent connect/disconnect resize
    the set being iterated) rather than protecting against OS threads.
    """

    def __init__(self) -> None:
        self._rooms: dict[UUID, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, workspace_id: UUID, websocket: WebSocket) -> None:
        """Register an already-accepted socket under its workspace room.

        The caller is responsible for ``websocket.accept()`` and authentication
        before handing the socket over — the manager only tracks membership.
        """
        async with self._lock:
            self._rooms.setdefault(workspace_id, set()).add(websocket)

    async def disconnect(self, workspace_id: UUID, websocket: WebSocket) -> None:
        """Drop a socket from its workspace room, pruning the room when empty."""
        async with self._lock:
            room = self._rooms.get(workspace_id)
            if room is None:
                return
            room.discard(websocket)
            if not room:
                self._rooms.pop(workspace_id, None)

    async def broadcast(self, workspace_id: UUID, event: Mapping[str, Any]) -> None:
        """Send ``event`` as JSON to every live socket in ``workspace_id``'s room.

        Isolation is structural: only that workspace's sockets are addressed.
        Sends are best-effort — a socket that errors mid-send (peer vanished
        between the disconnect callback and this broadcast) is collected and
        pruned rather than aborting delivery to the rest of the room.
        """
        async with self._lock:
            targets = list(self._rooms.get(workspace_id, ()))

        if not targets:
            return

        payload = dict(event)
        dead: list[WebSocket] = []
        for websocket in targets:
            try:
                await websocket.send_json(payload)
            except Exception:  # a broken peer must not block delivery to the room
                dead.append(websocket)

        if dead:
            async with self._lock:
                room = self._rooms.get(workspace_id)
                if room is not None:
                    for websocket in dead:
                        room.discard(websocket)
                    if not room:
                        self._rooms.pop(workspace_id, None)

    def connection_count(self, workspace_id: UUID) -> int:
        """Number of live sockets currently registered for a workspace (tests/metrics)."""
        return len(self._rooms.get(workspace_id, ()))


@lru_cache(maxsize=1)
def get_connection_manager() -> ConnectionManager:
    """Return the process-wide board-push connection manager.

    Cached so every request/socket shares one registry; overridable in tests via
    ``app.dependency_overrides``.
    """
    return ConnectionManager()
