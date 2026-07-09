"""Root-mounted board-push WebSocket endpoint (slice RT-1).

``/ws`` is the one-way push channel the web board hook
(``apps/web/src/lib/realtime/use-board-realtime.ts``) opens: the server pushes
JSON envelopes (``{"type": ..., "task_id": ...}``) as board entities change and
the client maps the dotted event type onto TanStack Query keys to invalidate.
This slice lands the authenticated, per-workspace connection seam; the entity
change-feed that calls :meth:`ConnectionManager.broadcast` arrives in a later
slice.

WebSocket auth is deliberately hand-rolled rather than expressed as a
``Depends``: a dependency that raises ``HTTPException`` does **not** translate to
a WebSocket handshake rejection (Starlette only maps that for HTTP routes). So we
``accept()`` the socket, resolve the ``?token=`` credential ourselves, and
``close(1008)`` on failure — the policy-violation close code — exactly as the
spec's WS-auth contract requires.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status

from forge_api.auth.rbac import Permission, can
from forge_api.auth.service import (
    AuthenticationError,
    AuthService,
    get_auth_service,
)
from forge_api.deps import Principal
from forge_api.realtime.manager import ConnectionManager, get_connection_manager
from forge_api.realtime.spec_room import SpecRoomRegistry, get_spec_room_registry
from forge_spec import SpecNotFoundError

router = APIRouter(tags=["realtime"])

#: RFC 6455 policy-violation close code — used for auth failures on an
#: already-accepted socket.
WS_POLICY_VIOLATION = 1008


def _extract_ws_token(websocket: WebSocket) -> str | None:
    """Pull the bearer credential from ``?token=`` or the WS subprotocol header.

    Browsers cannot set ``Authorization`` on a WebSocket handshake, so the token
    rides the query string (matching the web hook). The
    ``Sec-WebSocket-Protocol`` header is accepted as a fallback for clients that
    prefer to smuggle the credential through the subprotocol negotiation.
    """
    token = websocket.query_params.get("token")
    if token:
        return token.strip()
    protocol = websocket.headers.get("sec-websocket-protocol")
    if protocol:
        # A subprotocol offer may be a comma-separated list; the credential is
        # the last non-empty entry ("<subprotocol>, <token>" or just "<token>").
        candidate = protocol.split(",")[-1].strip()
        if candidate:
            return candidate
    return None


def _authenticate_ws(service: AuthService, token: str | None) -> Principal | None:
    """Resolve a WS token to a :class:`Principal`, or ``None`` when unauthenticated."""
    if not token:
        return None
    try:
        return service.authenticate(token)
    except AuthenticationError:
        return None


@router.websocket("/ws")
async def board_ws(
    websocket: WebSocket,
    manager: Annotated[ConnectionManager, Depends(get_connection_manager)],
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> None:
    """Authenticated, per-workspace board-push socket.

    Accepts the socket, authenticates the ``?token=`` credential, then registers
    the connection under the caller's ``workspace_id`` and blocks receiving until
    the peer disconnects. Broadcasts scoped to that workspace reach this socket.
    """
    await websocket.accept()

    principal = _authenticate_ws(service, _extract_ws_token(websocket))
    if principal is None:
        # A dependency raising HTTPException does NOT reject a WebSocket, so we
        # close explicitly after accepting.
        await websocket.close(code=WS_POLICY_VIOLATION)
        return

    workspace_id = principal.workspace_id
    await manager.connect(workspace_id, websocket)
    try:
        # Board push is server->client only; we drain inbound frames so the
        # socket stays open (and detect disconnects) but ignore their content.
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(workspace_id, websocket)


@router.websocket("/ws/spec/{spec_id}")
async def spec_collab_ws(
    websocket: WebSocket,
    spec_id: uuid.UUID,
    rooms: Annotated[SpecRoomRegistry, Depends(get_spec_room_registry)],
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> None:
    """Collaborative spec-editing socket (Yjs binary sync protocol).

    Auth mirrors ``/ws``: a missing/invalid ``?token=`` credential is rejected by
    accepting the socket then closing 1008 (a ``Depends`` raising
    ``HTTPException`` cannot reject a WebSocket handshake). A spec the caller's
    workspace does not have is denied *before* accept with a 404 — the engine
    seed raises :class:`SpecNotFoundError`, surfaced as an HTTP denial response.

    Write gating: any authenticated principal may connect and observe, but a
    principal without :attr:`Permission.WRITE` that sends a doc-mutating frame is
    a policy violation and is closed 1008 (mutations are rejected, never applied).
    """
    principal = _authenticate_ws(service, _extract_ws_token(websocket))
    if principal is None:
        await websocket.accept()
        await websocket.close(code=WS_POLICY_VIOLATION)
        return

    # Resolve (and lazily seed) the room BEFORE accept so an unknown spec is a
    # 404 handshake denial rather than a post-accept close.
    try:
        room = await rooms.get_or_create(principal.workspace_id, spec_id)
    except SpecNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    can_write = can(principal.role, Permission.WRITE)
    await websocket.accept()
    await room.connect(websocket)
    try:
        while True:
            data = await websocket.receive_bytes()
            allowed = await room.receive(
                websocket, data, can_write=can_write, user_id=principal.user_id
            )
            if not allowed:
                # A READ-only principal attempted a write — policy violation.
                await websocket.close(code=WS_POLICY_VIOLATION)
                return
    except WebSocketDisconnect:
        pass
    finally:
        await room.disconnect(websocket)
        await rooms.release(room)
