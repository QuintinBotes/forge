"""Tests for the realtime contracts module (RT-0).

The board realtime hook (``apps/web/src/lib/realtime/use-board-realtime.ts``)
dispatches on the raw dotted ``type`` string of each event; these tests pin
``RealtimeEventType`` to exactly the strings that hook's ``queryKeysForEvent``
expects (``task.*`` / ``incident.*`` / ``epic.*``), plus the ``run.*`` /
``approval.*`` values reserved for the run-trace and approval-UI slices.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from forge_contracts import (
    Broadcaster,
    CursorRange,
    PresenceState,
    RealtimeEvent,
    RealtimeEventType,
)

# The literal strings the web hook must keep working against. Any change here
# is a breaking change to apps/web/src/lib/realtime/use-board-realtime.ts.
EXPECTED_CLIENT_STRINGS = {
    "task.created",
    "task.updated",
    "task.deleted",
    "task.status_changed",
    "incident.created",
    "incident.updated",
    "incident.state_changed",
    "epic.created",
    "epic.updated",
    "run.started",
    "run.updated",
    "run.completed",
    "run.failed",
    "approval.requested",
    "approval.decided",
}


def test_event_type_values_match_web_hook_strings() -> None:
    actual = {member.value for member in RealtimeEventType}
    assert actual == EXPECTED_CLIENT_STRINGS


@pytest.mark.parametrize(
    "prefix",
    ["task", "incident", "epic", "run", "approval"],
)
def test_every_prefix_the_hook_branches_on_has_at_least_one_event(prefix: str) -> None:
    assert any(member.value.startswith(f"{prefix}.") for member in RealtimeEventType)


def test_realtime_event_round_trips_through_json() -> None:
    workspace_id = uuid.uuid4()
    task_id = uuid.uuid4()
    event = RealtimeEvent(
        type=RealtimeEventType.TASK_UPDATED,
        workspace_id=workspace_id,
        task_id=task_id,
        payload={"status": "in_progress"},
    )

    restored = RealtimeEvent.model_validate_json(event.model_dump_json())

    assert restored == event
    assert restored.type is RealtimeEventType.TASK_UPDATED
    assert restored.workspace_id == workspace_id
    assert restored.task_id == task_id
    assert restored.incident_id is None
    assert restored.payload == {"status": "in_progress"}


def test_realtime_event_type_field_serializes_as_the_dotted_string() -> None:
    event = RealtimeEvent(
        type=RealtimeEventType.INCIDENT_STATE_CHANGED,
        workspace_id=uuid.uuid4(),
    )
    dumped = event.model_dump(mode="json")
    assert dumped["type"] == "incident.state_changed"


def test_realtime_event_requires_type_and_workspace_id() -> None:
    with pytest.raises(ValidationError):
        RealtimeEvent()  # type: ignore[call-arg]


def test_realtime_event_rejects_unknown_event_type() -> None:
    with pytest.raises(ValidationError):
        RealtimeEvent(type="not.a.real.event", workspace_id=uuid.uuid4())


def test_presence_state_round_trips_with_optional_cursor() -> None:
    presence = PresenceState(
        user_id="u-1",
        display_name="Ada Lovelace",
        color="#6366f1",
        cursor=CursorRange(anchor=3, head=9),
    )
    restored = PresenceState.model_validate_json(presence.model_dump_json())
    assert restored == presence
    assert restored.cursor is not None
    assert restored.cursor.anchor == 3
    assert restored.cursor.head == 9


def test_presence_state_cursor_is_optional() -> None:
    presence = PresenceState(user_id="u-2", display_name="Grace Hopper", color="#22c55e")
    assert presence.cursor is None


def test_broadcaster_is_a_runtime_checkable_protocol() -> None:
    class InMemoryBroadcaster:
        async def publish(self, topic: str, event: RealtimeEvent) -> None:
            return None

        def subscribe(self, topic: str):
            async def _gen():
                if False:
                    yield None  # pragma: no cover - shape only

            return _gen()

    assert isinstance(InMemoryBroadcaster(), Broadcaster)
