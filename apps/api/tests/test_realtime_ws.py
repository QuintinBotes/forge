"""Slice RT-1 — server board-push WebSocket endpoint + connection registry.

Two layers are exercised:

* the transport-only :class:`ConnectionManager` — a broadcast reaches every
  socket in the target workspace room and *only* that room (tenant isolation);
* the root-mounted ``/ws`` route — it rejects an unauthenticated / bad-token
  handshake with the 1008 policy-violation close and accepts a valid principal.

WS auth is hand-rolled (a ``Depends`` raising ``HTTPException`` cannot reject a
WebSocket), so the route ``accept()``s first and ``close(1008)``s on failure —
these tests pin exactly that behaviour.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from forge_api.auth.service import AuthService, get_auth_service
from forge_api.main import create_app
from forge_api.realtime.manager import ConnectionManager, get_connection_manager
from forge_contracts import UserRole

WS_A = uuid.uuid4()
WS_B = uuid.uuid4()


# --------------------------------------------------------------------------- #
# ConnectionManager: broadcast fan-out + workspace isolation                   #
# --------------------------------------------------------------------------- #


class _RecordingSocket:
    """Minimal WebSocket stand-in that records the JSON payloads it receives."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)


async def test_broadcast_reaches_same_workspace_only() -> None:
    manager = ConnectionManager()
    a1, a2 = _RecordingSocket(), _RecordingSocket()
    b1 = _RecordingSocket()

    await manager.connect(WS_A, a1)  # type: ignore[arg-type]
    await manager.connect(WS_A, a2)  # type: ignore[arg-type]
    await manager.connect(WS_B, b1)  # type: ignore[arg-type]

    event = {"type": "task.updated", "task_id": str(uuid.uuid4())}
    await manager.broadcast(WS_A, event)

    # Both workspace-A sockets receive it; the workspace-B socket never does.
    assert a1.sent == [event]
    assert a2.sent == [event]
    assert b1.sent == []


async def test_disconnect_removes_socket_from_room() -> None:
    manager = ConnectionManager()
    sock = _RecordingSocket()

    await manager.connect(WS_A, sock)  # type: ignore[arg-type]
    assert manager.connection_count(WS_A) == 1

    await manager.disconnect(WS_A, sock)  # type: ignore[arg-type]
    assert manager.connection_count(WS_A) == 0

    # A broadcast after disconnect delivers to nobody (no error, no delivery).
    await manager.broadcast(WS_A, {"type": "task.created"})
    assert sock.sent == []


async def test_broadcast_prunes_broken_socket() -> None:
    manager = ConnectionManager()

    class _BrokenSocket(_RecordingSocket):
        async def send_json(self, payload: dict[str, Any]) -> None:
            raise RuntimeError("peer gone")

    good, broken = _RecordingSocket(), _BrokenSocket()
    await manager.connect(WS_A, good)  # type: ignore[arg-type]
    await manager.connect(WS_A, broken)  # type: ignore[arg-type]

    await manager.broadcast(WS_A, {"type": "task.updated"})

    # The good socket still received the event; the broken one was pruned.
    assert good.sent == [{"type": "task.updated"}]
    assert manager.connection_count(WS_A) == 1


# --------------------------------------------------------------------------- #
# /ws route: authenticated per-workspace handshake                             #
# --------------------------------------------------------------------------- #


@pytest.fixture
def auth_service() -> AuthService:
    """A hermetic in-memory auth service (no Postgres) for minting WS tokens."""
    return AuthService(secret_key=b"5" * 32)


@pytest.fixture
def manager() -> ConnectionManager:
    """A fresh, test-owned connection registry the ``/ws`` route registers into."""
    return ConnectionManager()


@pytest.fixture
def app_client(auth_service: AuthService, manager: ConnectionManager) -> TestClient:
    """A TestClient whose ``/ws`` route uses our auth service + shared manager."""
    app = create_app()
    app.dependency_overrides[get_auth_service] = lambda: auth_service
    app.dependency_overrides[get_connection_manager] = lambda: manager
    return TestClient(app)


def _mint(service: AuthService, workspace_id: uuid.UUID, role: UserRole) -> str:
    _, token = service.bootstrap_key(workspace_id=workspace_id, name=role.value, role=role)
    return token


def test_ws_rejects_missing_token(app_client: TestClient) -> None:
    with (
        app_client.websocket_connect("/ws") as ws,
        pytest.raises(WebSocketDisconnect) as exc,
    ):
        ws.receive_text()
    assert exc.value.code == 1008


def test_ws_rejects_bad_token(app_client: TestClient) -> None:
    with (
        app_client.websocket_connect("/ws?token=not-a-real-key") as ws,
        pytest.raises(WebSocketDisconnect) as exc,
    ):
        ws.receive_text()
    assert exc.value.code == 1008


def test_ws_accepts_valid_principal(app_client: TestClient, auth_service: AuthService) -> None:
    token = _mint(auth_service, WS_A, UserRole.MEMBER)
    with app_client.websocket_connect(f"/ws?token={token}") as ws:
        # An accepted socket stays open: a client->server frame does not raise,
        # and no immediate 1008 disconnect arrives.
        ws.send_text("ping")
        ws.close()


def test_ws_accepts_token_via_subprotocol_header(
    app_client: TestClient, auth_service: AuthService
) -> None:
    token = _mint(auth_service, WS_A, UserRole.MEMBER)
    # Browsers cannot set Authorization on a WS handshake; the token may ride the
    # Sec-WebSocket-Protocol negotiation instead of the query string.
    with app_client.websocket_connect("/ws", subprotocols=[token]) as ws:
        ws.send_text("ping")
        ws.close()


def test_ws_broadcast_reaches_connected_client(
    app_client: TestClient, auth_service: AuthService, manager: ConnectionManager
) -> None:
    """End-to-end: a broadcast on the app's manager reaches a live /ws client.

    ``ws.portal`` is the anyio blocking portal driving the app's event loop, so
    the async ``manager.broadcast`` is invoked on the same loop the socket lives
    on. By the time the connect context yields, the route has parked in
    ``receive_text()`` — so the socket is already registered when we broadcast.
    """
    token = _mint(auth_service, WS_A, UserRole.MEMBER)
    with app_client.websocket_connect(f"/ws?token={token}") as ws:
        event = {"type": "task.updated", "task_id": str(uuid.uuid4())}
        ws.portal.call(manager.broadcast, WS_A, event)
        assert ws.receive_json() == event
        ws.close()


def test_ws_broadcast_isolated_across_workspaces(
    app_client: TestClient, auth_service: AuthService, manager: ConnectionManager
) -> None:
    """A workspace-A client never receives a workspace-B broadcast."""
    token = _mint(auth_service, WS_A, UserRole.MEMBER)
    with app_client.websocket_connect(f"/ws?token={token}") as ws:
        # Broadcast to a *different* workspace: the A client must see nothing,
        # then the A broadcast must arrive — proving the first was truly isolated.
        ws.portal.call(manager.broadcast, WS_B, {"type": "task.created"})
        ws.portal.call(manager.broadcast, WS_A, {"type": "task.updated"})
        assert ws.receive_json() == {"type": "task.updated"}
        ws.close()
