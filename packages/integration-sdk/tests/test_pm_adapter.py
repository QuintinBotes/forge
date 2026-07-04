"""Tests for the external PM adapter surface (plan Task 1.13 / spec contract)."""

from __future__ import annotations

from forge_contracts import (
    Direction,
    ExternalTask,
    HealthResult,
    Priority,
    TaskDTO,
    TaskStatus,
)
from forge_integrations import BasePMAdapter, GenericPMAdapter


def test_map_status_in_and_out() -> None:
    a = GenericPMAdapter()
    assert a.map_status("In Progress", Direction.IN) == TaskStatus.IN_PROGRESS.value
    assert a.map_status("Done", Direction.IN) == TaskStatus.DONE.value
    assert a.map_status("closed", Direction.IN) == TaskStatus.DONE.value
    # OUT maps a forge status back to the external vocabulary.
    assert a.map_status(TaskStatus.IN_PROGRESS.value, Direction.OUT) == "In Progress"
    assert a.map_status(TaskStatus.DONE.value, Direction.OUT) == "Done"


def test_map_status_unknown_falls_back_to_default() -> None:
    a = GenericPMAdapter()
    assert a.map_status("Some Weird Column", Direction.IN) == TaskStatus.BACKLOG.value
    assert a.map_status("not_a_real_status", Direction.OUT) == "To Do"


def test_map_priority_both_directions() -> None:
    a = GenericPMAdapter()
    assert a.map_priority("Highest", Direction.IN) == Priority.URGENT.value
    assert a.map_priority("Low", Direction.IN) == Priority.LOW.value
    assert a.map_priority(Priority.URGENT.value, Direction.OUT) == "Urgent"


def test_map_fields_renames_in_and_out() -> None:
    a = GenericPMAdapter()
    external = {"summary": "Title here", "assignee": "alice"}
    forge_fields = a.map_fields(external, Direction.IN)
    assert forge_fields["title"] == "Title here"
    assert forge_fields["assignee_name"] == "alice"
    # Round-trip back out.
    back = a.map_fields(forge_fields, Direction.OUT)
    assert back["summary"] == "Title here"
    assert back["assignee"] == "alice"


def test_map_fields_passes_unknown_keys_through() -> None:
    a = GenericPMAdapter()
    assert a.map_fields({"custom_x": 1}, Direction.IN) == {"custom_x": 1}


def test_sync_in_produces_forge_task() -> None:
    a = GenericPMAdapter()
    ext = ExternalTask(
        external_id="JIRA-101",
        system="generic",
        title="Add pagination",
        status="In Progress",
        priority="High",
        fields={"description": "details"},
    )
    task = a.sync_in(ext)
    assert isinstance(task, TaskDTO)
    assert task.key == "JIRA-101"
    assert task.title == "Add pagination"
    assert task.status is TaskStatus.IN_PROGRESS
    assert task.priority is Priority.HIGH
    assert task.description == "details"


def test_sync_out_produces_external_task() -> None:
    a = GenericPMAdapter()
    task = TaskDTO(
        key="TASK-9",
        title="Fix bug",
        status=TaskStatus.DONE,
        priority=Priority.URGENT,
    )
    ext = a.sync_out(task)
    assert isinstance(ext, ExternalTask)
    assert ext.external_id == "TASK-9"
    assert ext.system == "generic"
    assert ext.title == "Fix bug"
    assert ext.status == "Done"
    assert ext.priority == "Urgent"


def test_sync_roundtrip_preserves_core_fields() -> None:
    a = GenericPMAdapter()
    ext = ExternalTask(
        external_id="EXT-7",
        system="generic",
        title="Roundtrip",
        status="To Do",
        priority="Medium",
    )
    back = a.sync_out(a.sync_in(ext))
    assert back.external_id == "EXT-7"
    assert back.title == "Roundtrip"
    assert back.status == "To Do"
    assert back.priority == "Medium"


def test_subscribe_records_event() -> None:
    a = GenericPMAdapter()
    from forge_contracts import WebhookEvent

    event = WebhookEvent(source="generic", event_type="issue.updated", payload={"id": 1})
    assert a.subscribe(event) is None
    assert a.subscriptions == [event]


def test_get_connection_health() -> None:
    a = GenericPMAdapter()
    health = a.get_connection_health()
    assert isinstance(health, HealthResult)
    assert health.healthy is True


def test_base_adapter_is_subclassable_for_custom_systems() -> None:
    class JiraLike(BasePMAdapter):
        system = "jira"

    a = JiraLike()
    assert a.sync_out(TaskDTO(key="J-1", title="x")).system == "jira"
