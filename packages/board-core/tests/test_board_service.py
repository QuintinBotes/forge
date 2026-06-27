"""Tests for the in-memory BoardService domain implementation (plan Task 1.5).

Covers: CRUD for every board entity, status workflow enforcement, bulk ops,
saved-filter queries, the dependency graph with cycle detection, and structural
conformance to the frozen ``BoardService`` protocol.
"""

from __future__ import annotations

import uuid

import pytest

from forge_board import InMemoryBoardService
from forge_board.exceptions import EntityNotFoundError, InvalidStatusTransitionError
from forge_contracts import (
    BoardFilter,
    BoardService,
    BulkUpdate,
    CycleError,
    EpicDTO,
    IncidentDTO,
    MilestoneDTO,
    Priority,
    SprintDTO,
    TaskDTO,
    TaskKind,
    TaskStatus,
)

PROJECT_A = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
PROJECT_B = uuid.UUID("00000000-0000-0000-0000-0000000000b2")


@pytest.fixture
def svc() -> InMemoryBoardService:
    return InMemoryBoardService()


def _task(svc: InMemoryBoardService, title: str = "Do thing", **kw: object) -> TaskDTO:
    data = TaskDTO(title=title, project_id=kw.pop("project_id", PROJECT_A), **kw)  # type: ignore[arg-type]
    return svc.create_task(data)


# --------------------------------------------------------------------------- #
# Protocol conformance                                                         #
# --------------------------------------------------------------------------- #


def test_service_conforms_to_board_service_protocol(svc: InMemoryBoardService) -> None:
    assert isinstance(svc, BoardService)


# --------------------------------------------------------------------------- #
# Task CRUD                                                                    #
# --------------------------------------------------------------------------- #


def test_create_task_assigns_id_key_and_timestamps(svc: InMemoryBoardService) -> None:
    task = _task(svc, "Implement login")
    assert task.id is not None
    assert task.key == "TASK-1"
    assert task.created_at is not None
    assert task.updated_at is not None
    assert task.status is TaskStatus.BACKLOG


def test_keys_increment_per_creation(svc: InMemoryBoardService) -> None:
    t1 = _task(svc, "one")
    t2 = _task(svc, "two")
    assert t1.key == "TASK-1"
    assert t2.key == "TASK-2"


def test_get_task_round_trips(svc: InMemoryBoardService) -> None:
    created = _task(svc, "round trip")
    assert created.id is not None
    fetched = svc.get_task(created.id)
    assert fetched.id == created.id
    assert fetched.title == "round trip"


def test_get_missing_task_raises(svc: InMemoryBoardService) -> None:
    with pytest.raises(EntityNotFoundError):
        svc.get_task(uuid.uuid4())


def test_update_task_preserves_identity_and_bumps_updated_at(svc: InMemoryBoardService) -> None:
    created = _task(svc, "original")
    assert created.id is not None
    edit = created.model_copy(deep=True)
    edit.title = "edited"
    edit.priority = Priority.HIGH
    updated = svc.update_task(created.id, edit)
    assert updated.id == created.id
    assert updated.key == created.key  # key is immutable across updates
    assert updated.created_at == created.created_at
    assert updated.title == "edited"
    assert updated.priority is Priority.HIGH


def test_update_missing_task_raises(svc: InMemoryBoardService) -> None:
    with pytest.raises(EntityNotFoundError):
        svc.update_task(uuid.uuid4(), TaskDTO(title="ghost"))


def test_delete_task(svc: InMemoryBoardService) -> None:
    created = _task(svc, "to delete")
    assert created.id is not None
    svc.delete_task(created.id)
    with pytest.raises(EntityNotFoundError):
        svc.get_task(created.id)


def test_stored_tasks_are_isolated_copies(svc: InMemoryBoardService) -> None:
    created = _task(svc, "isolated")
    assert created.id is not None
    created.title = "mutated outside"
    refetched = svc.get_task(created.id)
    assert refetched.title == "isolated"


# --------------------------------------------------------------------------- #
# Status workflow                                                              #
# --------------------------------------------------------------------------- #


def test_set_status_valid_transition(svc: InMemoryBoardService) -> None:
    task = _task(svc, "status")
    assert task.id is not None
    moved = svc.set_status(task.id, TaskStatus.READY)
    assert moved.status is TaskStatus.READY
    assert moved.updated_at is not None


def test_set_status_invalid_transition_raises(svc: InMemoryBoardService) -> None:
    task = _task(svc, "status")
    assert task.id is not None
    with pytest.raises(InvalidStatusTransitionError):
        svc.set_status(task.id, TaskStatus.DONE)


def test_set_status_missing_task_raises(svc: InMemoryBoardService) -> None:
    with pytest.raises(EntityNotFoundError):
        svc.set_status(uuid.uuid4(), TaskStatus.READY)


# --------------------------------------------------------------------------- #
# Bulk update                                                                  #
# --------------------------------------------------------------------------- #


def test_bulk_update_applies_status_and_priority(svc: InMemoryBoardService) -> None:
    t1 = _task(svc, "b1")
    t2 = _task(svc, "b2")
    assert t1.id is not None and t2.id is not None
    updates = [
        BulkUpdate(task_id=t1.id, status=TaskStatus.READY, priority=Priority.URGENT),
        BulkUpdate(task_id=t2.id, status=TaskStatus.READY),
    ]
    result = svc.bulk_update(updates)
    assert {r.status for r in result} == {TaskStatus.READY}
    assert svc.get_task(t1.id).priority is Priority.URGENT


def test_bulk_update_is_atomic_on_invalid_transition(svc: InMemoryBoardService) -> None:
    t1 = _task(svc, "ok")
    t2 = _task(svc, "bad")
    assert t1.id is not None and t2.id is not None
    updates = [
        BulkUpdate(task_id=t1.id, status=TaskStatus.READY),
        BulkUpdate(task_id=t2.id, status=TaskStatus.DONE),  # illegal jump
    ]
    with pytest.raises(InvalidStatusTransitionError):
        svc.bulk_update(updates)
    # Nothing applied: t1 still in its original status.
    assert svc.get_task(t1.id).status is TaskStatus.BACKLOG


def test_bulk_update_missing_task_is_atomic(svc: InMemoryBoardService) -> None:
    t1 = _task(svc, "ok")
    assert t1.id is not None
    updates = [
        BulkUpdate(task_id=t1.id, status=TaskStatus.READY),
        BulkUpdate(task_id=uuid.uuid4(), status=TaskStatus.READY),
    ]
    with pytest.raises(EntityNotFoundError):
        svc.bulk_update(updates)
    assert svc.get_task(t1.id).status is TaskStatus.BACKLOG


# --------------------------------------------------------------------------- #
# Dependencies + cycle detection                                              #
# --------------------------------------------------------------------------- #


def test_dependency_add_records_edge(svc: InMemoryBoardService) -> None:
    a = _task(svc, "a")
    b = _task(svc, "b")
    assert a.id is not None and b.id is not None
    svc.dependency_add(a.id, b.id)
    assert b.id in svc.get_task(a.id).depends_on


def test_dependency_cycle_raises(svc: InMemoryBoardService) -> None:
    a = _task(svc, "a")
    b = _task(svc, "b")
    assert a.id is not None and b.id is not None
    svc.dependency_add(a.id, b.id)
    with pytest.raises(CycleError):
        svc.dependency_add(b.id, a.id)


def test_transitive_dependency_cycle_raises(svc: InMemoryBoardService) -> None:
    a = _task(svc, "a")
    b = _task(svc, "b")
    c = _task(svc, "c")
    assert a.id is not None and b.id is not None and c.id is not None
    svc.dependency_add(a.id, b.id)
    svc.dependency_add(b.id, c.id)
    with pytest.raises(CycleError):
        svc.dependency_add(c.id, a.id)


def test_self_dependency_raises(svc: InMemoryBoardService) -> None:
    a = _task(svc, "a")
    assert a.id is not None
    with pytest.raises(CycleError):
        svc.dependency_add(a.id, a.id)


def test_dependency_add_missing_task_raises(svc: InMemoryBoardService) -> None:
    a = _task(svc, "a")
    assert a.id is not None
    with pytest.raises(EntityNotFoundError):
        svc.dependency_add(a.id, uuid.uuid4())


def test_update_task_with_dependency_cycle_raises(svc: InMemoryBoardService) -> None:
    a = _task(svc, "a")
    b = _task(svc, "b")
    assert a.id is not None and b.id is not None
    svc.dependency_add(a.id, b.id)
    # Try to make b depend on a via a full-replace update -> cycle.
    edit = svc.get_task(b.id)
    edit.depends_on = [a.id]
    with pytest.raises(CycleError):
        svc.update_task(b.id, edit)


# --------------------------------------------------------------------------- #
# Filters / saved-filter queries                                              #
# --------------------------------------------------------------------------- #


def test_list_tasks_filter_by_status(svc: InMemoryBoardService) -> None:
    t1 = _task(svc, "one")
    _task(svc, "two")
    assert t1.id is not None
    svc.set_status(t1.id, TaskStatus.READY)
    ready = svc.list_tasks(BoardFilter(statuses=[TaskStatus.READY.value]))
    assert [t.id for t in ready] == [t1.id]


def test_list_tasks_filter_by_project(svc: InMemoryBoardService) -> None:
    _task(svc, "a", project_id=PROJECT_A)
    _task(svc, "b", project_id=PROJECT_B)
    only_b = svc.list_tasks(BoardFilter(project_id=PROJECT_B))
    assert len(only_b) == 1
    assert only_b[0].project_id == PROJECT_B


def test_list_tasks_filter_by_text(svc: InMemoryBoardService) -> None:
    _task(svc, "Implement OAuth login")
    _task(svc, "Fix flaky test")
    hits = svc.list_tasks(BoardFilter(text="oauth"))
    assert len(hits) == 1
    assert "OAuth" in hits[0].title


def test_list_tasks_filter_by_kind_and_priority(svc: InMemoryBoardService) -> None:
    _task(svc, "bug", kind=TaskKind.BUG, priority=Priority.HIGH)
    _task(svc, "feature", kind=TaskKind.FEATURE, priority=Priority.LOW)
    bugs = svc.list_tasks(BoardFilter(kinds=[TaskKind.BUG]))
    assert len(bugs) == 1
    high = svc.list_tasks(BoardFilter(priorities=[Priority.HIGH]))
    assert len(high) == 1


def test_list_tasks_pagination(svc: InMemoryBoardService) -> None:
    for i in range(5):
        _task(svc, f"t{i}")
    page = svc.list_tasks(BoardFilter(limit=2, offset=1))
    assert len(page) == 2


def test_list_tasks_no_filter_returns_all(svc: InMemoryBoardService) -> None:
    _task(svc, "a")
    _task(svc, "b")
    assert len(svc.list_tasks()) == 2


# --------------------------------------------------------------------------- #
# Epic / Sprint / Milestone / Incident CRUD                                    #
# --------------------------------------------------------------------------- #


def test_epic_crud(svc: InMemoryBoardService) -> None:
    epic = svc.create_epic(EpicDTO(title="Auth epic", project_id=PROJECT_A))
    assert epic.id is not None
    assert epic.key == "EPIC-1"
    fetched = svc.get_epic(epic.id)
    assert fetched.title == "Auth epic"
    edit = fetched.model_copy(deep=True)
    edit.title = "Auth epic v2"
    updated = svc.update_epic(epic.id, edit)
    assert updated.title == "Auth epic v2"
    assert updated.key == "EPIC-1"
    assert len(svc.list_epics()) == 1
    svc.delete_epic(epic.id)
    with pytest.raises(EntityNotFoundError):
        svc.get_epic(epic.id)


def test_sprint_crud(svc: InMemoryBoardService) -> None:
    sprint = svc.create_sprint(SprintDTO(name="Sprint 1", project_id=PROJECT_A))
    assert sprint.id is not None
    assert svc.get_sprint(sprint.id).name == "Sprint 1"
    edit = sprint.model_copy(deep=True)
    edit.goal = "ship board"
    assert svc.update_sprint(sprint.id, edit).goal == "ship board"
    assert len(svc.list_sprints()) == 1
    svc.delete_sprint(sprint.id)
    with pytest.raises(EntityNotFoundError):
        svc.get_sprint(sprint.id)


def test_milestone_crud(svc: InMemoryBoardService) -> None:
    ms = svc.create_milestone(MilestoneDTO(name="GA", project_id=PROJECT_A))
    assert ms.id is not None
    assert svc.get_milestone(ms.id).name == "GA"
    edit = ms.model_copy(deep=True)
    edit.description = "general availability"
    assert svc.update_milestone(ms.id, edit).description == "general availability"
    assert len(svc.list_milestones()) == 1
    svc.delete_milestone(ms.id)
    with pytest.raises(EntityNotFoundError):
        svc.get_milestone(ms.id)


def test_incident_crud_and_key(svc: InMemoryBoardService) -> None:
    inc = svc.create_incident(IncidentDTO(title="DB down", project_id=PROJECT_A))
    assert inc.id is not None
    assert inc.key == "INC-1"
    assert svc.get_incident(inc.id).title == "DB down"
    edit = inc.model_copy(deep=True)
    edit.title = "DB degraded"
    assert svc.update_incident(inc.id, edit).title == "DB degraded"
    assert len(svc.list_incidents()) == 1
    svc.delete_incident(inc.id)
    with pytest.raises(EntityNotFoundError):
        svc.get_incident(inc.id)


def test_list_epics_filter_by_project(svc: InMemoryBoardService) -> None:
    svc.create_epic(EpicDTO(title="a", project_id=PROJECT_A))
    svc.create_epic(EpicDTO(title="b", project_id=PROJECT_B))
    assert len(svc.list_epics(BoardFilter(project_id=PROJECT_A))) == 1
