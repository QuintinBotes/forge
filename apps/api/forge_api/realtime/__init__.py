"""Realtime push + collaborative-editing infrastructure for the Forge API.

Slice RT-1 lands the server WebSocket seam: a per-workspace
:class:`~forge_api.realtime.manager.ConnectionManager` and the root-mounted
``/ws`` board-push endpoint (``forge_api.routers.realtime``). Later slices add
the ``/ws/spec/{spec_id}`` collaborative-editing channel on top of the same
connection registry.
"""

from __future__ import annotations

from forge_api.realtime.broadcaster import (
    InProcessBroadcaster,
    RedisBroadcaster,
    emit_event,
    get_broadcaster,
)
from forge_api.realtime.manager import ConnectionManager, get_connection_manager

__all__ = [
    "ConnectionManager",
    "InProcessBroadcaster",
    "RedisBroadcaster",
    "emit_event",
    "get_broadcaster",
    "get_connection_manager",
]
