"""Slice RT-2 — realtime event publisher + fan-out from the write paths.

The in-process path is exercised end to end: a board write handler emits a
``RealtimeEvent`` which the :class:`InProcessBroadcaster` forwards to the
process-wide :class:`ConnectionManager`, reaching a socket registered under the
caller's workspace. A ``_RecordingSocket`` (registered on the app's event loop
via the TestClient portal, so the manager's lock binds to that one loop) stands
in for a live ``/ws`` client and captures the JSON envelopes it receives.

The Redis fan-out path has no in-repo fake (repo convention: infra-dependent
tests park without live infra), so its round-trip test SKIPS when no Redis is
reachable and only asserts the publish -> subscriber-loop -> manager delivery
when one is.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import anyio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forge_api.main import create_app
from forge_api.realtime.broadcaster import RedisBroadcaster
from forge_api.realtime.manager import ConnectionManager, get_connection_manager
from forge_api.routers.board import get_board_service
from forge_api.settings import get_settings
from forge_board import InMemoryBoardService
from forge_contracts import RealtimeEvent, RealtimeEventType

# Matches the workspace the ``authenticate_app`` fixture's admin principal carries
# (``tests/conftest.py``), so a socket registered here shares the caller's room.
WORKSPACE = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


class _RecordingSocket:
    """WebSocket stand-in that records the JSON envelopes broadcast to it."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)


def _make_client(
    authenticate_app: Callable[..., FastAPI],
) -> tuple[TestClient, ConnectionManager, InMemoryBoardService]:
    app = create_app()
    authenticate_app(app)
    service = InMemoryBoardService()
    manager = ConnectionManager()
    app.dependency_overrides[get_board_service] = lambda: service
    # The default ``get_broadcaster`` depends on ``get_connection_manager``, so
    # overriding the manager makes the in-process broadcaster fan out into ours.
    app.dependency_overrides[get_connection_manager] = lambda: manager
    return TestClient(app), manager, service


def test_task_status_change_emits_event(
    authenticate_app: Callable[..., FastAPI],
) -> None:
    """A ``POST /board/tasks/{id}/status`` fans a ``task.status_changed`` out to the room."""
    client, manager, _ = _make_client(authenticate_app)
    socket = _RecordingSocket()

    with client:
        # Register on the app's event loop so the manager's asyncio.Lock binds to
        # the same loop the broadcast later runs on.
        client.portal.call(manager.connect, WORKSPACE, socket)  # type: ignore[arg-type]

        created = client.post(
            "/board/tasks", json={"title": "Ship it", "project_id": str(WORKSPACE)}
        ).json()
        # The create already fanned out a task.created; isolate the status event.
        socket.sent.clear()

        resp = client.post(f"/board/tasks/{created['id']}/status", json={"status": "ready"})
        assert resp.status_code == 200, resp.text

    assert len(socket.sent) == 1
    event = socket.sent[0]
    assert event["type"] == RealtimeEventType.TASK_STATUS_CHANGED.value
    assert event["type"] == "task.status_changed"
    assert event["task_id"] == created["id"]
    assert event["payload"] == {"status": "ready"}
    # workspace_id is an internal routing field and must not cross the wire.
    assert "workspace_id" not in event


def test_task_create_emits_event(
    authenticate_app: Callable[..., FastAPI],
) -> None:
    """Creating a task fans a ``task.created`` envelope carrying the new id."""
    client, manager, _ = _make_client(authenticate_app)
    socket = _RecordingSocket()

    with client:
        client.portal.call(manager.connect, WORKSPACE, socket)  # type: ignore[arg-type]
        created = client.post(
            "/board/tasks", json={"title": "New", "project_id": str(WORKSPACE)}
        ).json()

    assert [e["type"] for e in socket.sent] == [RealtimeEventType.TASK_CREATED.value]
    assert socket.sent[0]["task_id"] == created["id"]


def test_broadcast_is_workspace_isolated(
    authenticate_app: Callable[..., FastAPI],
) -> None:
    """A socket in a *foreign* workspace never observes another workspace's write."""
    client, manager, _ = _make_client(authenticate_app)
    foreign = _RecordingSocket()

    with client:
        # Register the socket under a DIFFERENT workspace than the caller's.
        client.portal.call(manager.connect, uuid.uuid4(), foreign)  # type: ignore[arg-type]
        client.post("/board/tasks", json={"title": "x", "project_id": str(WORKSPACE)})

    assert foreign.sent == []


# --------------------------------------------------------------------------- #
# Redis fan-out path — parked without a live Redis                             #
# --------------------------------------------------------------------------- #


def test_redis_broadcaster_roundtrip_or_park() -> None:
    """publish -> ``run_forever`` -> ConnectionManager, or SKIP without live Redis."""
    aioredis = pytest.importorskip("redis.asyncio")

    async def _run() -> None:
        client = aioredis.Redis.from_url(get_settings().redis_url)
        try:
            await client.ping()
        except Exception as exc:  # no live Redis on this host -> park cleanly
            await client.aclose()
            pytest.skip(f"no live Redis ({exc}); Redis fan-out path parked")

        manager = ConnectionManager()
        socket = _RecordingSocket()
        await manager.connect(WORKSPACE, socket)  # type: ignore[arg-type]
        broadcaster = RedisBroadcaster(client, manager)

        async with anyio.create_task_group() as tg:
            tg.start_soon(broadcaster.run_forever)
            with anyio.fail_after(5):
                event = RealtimeEvent(
                    type=RealtimeEventType.TASK_UPDATED,
                    workspace_id=WORKSPACE,
                    task_id=uuid.uuid4(),
                )
                # Give the psubscribe a beat to register before publishing.
                for _ in range(50):
                    await broadcaster.publish(str(WORKSPACE), event)
                    if socket.sent:
                        break
                    await anyio.sleep(0.05)
            tg.cancel_scope.cancel()

        await client.aclose()

        assert socket.sent, "published event never reached the connection manager"
        assert socket.sent[-1]["type"] == "task.updated"

    anyio.run(_run)
