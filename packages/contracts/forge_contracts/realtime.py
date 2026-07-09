"""Realtime vocabulary shared by the board WS (``/ws``) and spec collab WS
(``/ws/spec/{spec_id}``).

This module is the single source of truth for the dotted event-type strings
that cross the wire to the web client. ``RealtimeEventType`` values MUST stay
byte-for-byte identical to the strings
``apps/web/src/lib/realtime/use-board-realtime.ts`` branches on
(``queryKeysForEvent``: it prefix-matches ``task``/``incident``/``epic``/``run``/
``approval``, falling back to the task query keys for anything else). No
behavior lives here — this is transport shape only.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    """Shared base: tolerant of unknown keys, populatable by field name or alias."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


# --------------------------------------------------------------------------- #
# Event vocabulary                                                             #
# --------------------------------------------------------------------------- #


class RealtimeEventType(enum.StrEnum):
    """Dotted realtime event types.

    Values mirror the prefixes the board realtime hook dispatches on
    (``task.*`` -> ``["tasks"]``, ``incident.*`` -> ``["incidents"]``,
    ``epic.*`` -> ``["epics", "tasks"]``, ``run.*`` -> ``["runs"]``,
    ``approval.*`` -> ``["approvals"]``).
    """

    TASK_CREATED = "task.created"
    TASK_UPDATED = "task.updated"
    TASK_DELETED = "task.deleted"
    TASK_STATUS_CHANGED = "task.status_changed"

    INCIDENT_CREATED = "incident.created"
    INCIDENT_UPDATED = "incident.updated"
    INCIDENT_STATE_CHANGED = "incident.state_changed"

    EPIC_CREATED = "epic.created"
    EPIC_UPDATED = "epic.updated"

    RUN_STARTED = "run.started"
    RUN_UPDATED = "run.updated"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"

    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_DECIDED = "approval.decided"


class RealtimeEvent(_Model):
    """The envelope pushed over ``/ws`` (board) and ``/ws/spec/{spec_id}`` (collab).

    ``payload`` carries the event-specific body; the identifier fields are
    optional cross-references so a subscriber can filter/route without
    parsing ``payload``.
    """

    type: RealtimeEventType
    workspace_id: uuid.UUID
    task_id: uuid.UUID | None = None
    incident_id: uuid.UUID | None = None
    epic_id: uuid.UUID | None = None
    run_id: uuid.UUID | None = None
    approval_id: uuid.UUID | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Presence / cursors (spec collab)                                            #
# --------------------------------------------------------------------------- #


class CursorRange(_Model):
    """A collaborator's cursor or selection range within a spec document."""

    anchor: int
    head: int


class PresenceState(_Model):
    """A single collaborator's presence in a spec collab session."""

    user_id: str
    display_name: str
    color: str
    cursor: CursorRange | None = None


# --------------------------------------------------------------------------- #
# Pub/sub structural contract                                                 #
# --------------------------------------------------------------------------- #


@runtime_checkable
class Broadcaster(Protocol):
    """Topic pub/sub used to fan realtime events out to WS connections.

    Implementations: an in-process asyncio broadcaster (default) and an
    optional Redis-backed one (multi-process deployments).
    """

    async def publish(self, topic: str, event: RealtimeEvent) -> None: ...

    def subscribe(self, topic: str) -> AsyncIterator[RealtimeEvent]: ...


__all__ = [
    "Broadcaster",
    "CursorRange",
    "PresenceState",
    "RealtimeEvent",
    "RealtimeEventType",
]
