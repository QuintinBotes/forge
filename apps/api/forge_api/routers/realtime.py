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

from typing import Annotated

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from forge_api.auth.service import (
    AuthenticationError,
    AuthService,
    get_auth_service,
)
from forge_api.deps import Principal
from forge_api.realtime.manager import ConnectionManager, get_connection_manager

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
