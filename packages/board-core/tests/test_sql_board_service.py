"""Postgres integration tests for :class:`SqlAlchemyBoardService` (F01 persistence).

Exercises the DB-backed board repository against a real pgvector Postgres via the
shared ``pg_engine`` fixture (root ``conftest.py``): a full DTO round-trip for
every entity, the status workflow, bulk atomicity, the dependency graph + cycle
detection, saved-filter queries (filtering + ordering + pagination), key +
workspace uniqueness constraints, cross-workspace isolation, durability across
service instances, and structural conformance to the frozen ``BoardService``
protocol. Skips cleanly (parked) when no Postgres is reachable; runs under
``FORGE_TEST_DATABASE_URL`` (pgvector :5433) in the gate.

Each behaviour mirrors ``tests/test_board_service.py`` (the in-memory contract),
so both backends are proven to satisfy the same protocol identically.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from forge_board import SqlAlchemyBoardService
from forge_board.exceptions import EntityNotFoundError, InvalidStatusTransitionError
from forge_contracts import (
    BoardFilter,
    BoardService,
    BulkUpdate,
    CycleError,
    EpicDTO,
    IncidentDTO,
    KnowledgeScope,
    MilestoneDTO,
    Priority,
    RepoTarget,
    SprintDTO,
    TaskDTO,
    TaskKind,
    TaskStatus,
)
from forge_contracts.enums import IncidentSeverity, IncidentState
from forge_db.base import Base
from forge_db.models import Project, Task, TaskDependency, Workspace

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("pg_engine")]


@pytest.fixture
def factory(pg_engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    try:
        yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(pg_engine)


@pytest.fixture
def seed(factory: sessionmaker[Session]) -> dict[str, uuid.UUID]:
    """A workspace + two projects (A/B) + a second isolated workspace."""
    ws = uuid.uuid4()
    other_ws = uuid.uuid4()
    proj_a = uuid.uuid4()
    proj_b = uuid.uuid4()
    other_proj = uuid.uuid4()
    with factory() as session:
        session.add(Workspace(id=ws, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}"))
        session.add(Workspace(id=other_ws, name="Other", slug=f"other-{uuid.uuid4().hex[:8]}"))
        session.flush()
        session.add(Project(id=proj_a, workspace_id=ws, name="A", key=f"A{uuid.uuid4().hex[:4]}"))
        session.add(Project(id=proj_b, workspace_id=ws, name="B", key=f"B{uuid.uuid4().hex[:4]}"))
        session.add(
            Project(id=other_proj, workspace_id=other_ws, name="O", key=f"O{uuid.uuid4().hex[:4]}")
        )
        session.commit()
    return {
        "ws": ws,
        "other_ws": other_ws,
        "proj_a": proj_a,
        "proj_b": proj_b,
        "other_proj": other_proj,
    }


@pytest.fixture
def svc(
    factory: sessionmaker[Session], seed: dict[str, uuid.UUID]
) -> SqlAlchemyBoardService:
    return SqlAlchemyBoardService(factory, seed["ws"])


def _task(
    svc: SqlAlchemyBoardService, project: uuid.UUID, title: str = "Do thing", **kw
) -> TaskDTO:
    return svc.create_task(TaskDTO(title=title, project_id=project, **kw))


# --------------------------------------------------------------------------- #
# Protocol conformance                                                         #
# --------------------------------------------------------------------------- #


def test_service_conforms_to_board_service_protocol(svc: SqlAlchemyBoardService) -> None:
    assert isinstance(svc, BoardService)


# --------------------------------------------------------------------------- #
# Task CRUD + full round-trip                                                  #
# --------------------------------------------------------------------------- #


def test_create_task_assigns_id_key_and_timestamps(
    svc: SqlAlchemyBoardService, seed: dict[str, uuid.UUID]
) -> None:
    task = _task(svc, seed["proj_a"], "Implement login")
    assert task.id is not None
    assert task.key == "TASK-1"
    assert task.created_at is not None
    assert task.updated_at is not None
    assert task.status is TaskStatus.BACKLOG


def test_keys_increment_per_creation(
    svc: SqlAlchemyBoardService, seed: dict[str, uuid.UUID]
) -> None:
    t1 = _task(svc, seed["proj_a"], "one")
    t2 = _task(svc, seed["proj_a"], "two")
    assert t1.key == "TASK-1"
    assert t2.key == "TASK-2"


def test_task_full_round_trip(
    svc: SqlAlchemyBoardService, seed: dict[str, uuid.UUID]
) -> None:
    """Every DTO field — including nested models + None-able scopes — survives."""
    created = _task(
        svc,
        seed["proj_a"],
        "round trip",
        description="body",
        kind=TaskKind.BUG,
        priority=Priority.HIGH,
        estimate=5,
        labels=["x", "y"],
        repo_targets=[RepoTarget(repo="svc-a", base_branch="develop")],
        knowledge_scope=KnowledgeScope(repos=["r1"], freshness_min_hours=12),
        handoff_rules=None,
    )
    assert created.id is not None
    fetched = svc.get_task(created.id)
    assert fetched.id == created.id
    assert fetched.title == "round trip"
    assert fetched.description == "body"
    assert fetched.kind is TaskKind.BUG
    assert fetched.priority is Priority.HIGH
    assert fetched.estimate == 5
    assert fetched.labels == ["x", "y"]
    assert fetched.repo_targets[0].repo == "svc-a"
    assert fetched.repo_targets[0].base_branch == "develop"
    assert fetched.knowledge_scope is not None
    assert fetched.knowledge_scope.repos == ["r1"]
    assert fetched.knowledge_scope.freshness_min_hours == 12
    # ``handoff_rules=None`` round-trips as None (distinct from an empty object).
    assert fetched.handoff_rules is None


def test_get_missing_task_raises(svc: SqlAlchemyBoardService) -> None:
    with pytest.raises(EntityNotFoundError):
        svc.get_task(uuid.uuid4())


def test_update_task_preserves_identity_and_bumps_updated_at(
    svc: SqlAlchemyBoardService, seed: dict[str, uuid.UUID]
) -> None:
    created = _task(svc, seed["proj_a"], "original")
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


def test_update_missing_task_raises(svc: SqlAlchemyBoardService) -> None:
    with pytest.raises(EntityNotFoundError):
        svc.update_task(uuid.uuid4(), TaskDTO(title="ghost"))


def test_delete_task(svc: SqlAlchemyBoardService, seed: dict[str, uuid.UUID]) -> None:
    created = _task(svc, seed["proj_a"], "to delete")
    assert created.id is not None
    svc.delete_task(created.id)
    with pytest.raises(EntityNotFoundError):
        svc.get_task(created.id)


def test_persistence_across_service_instances(
    factory: sessionmaker[Session], seed: dict[str, uuid.UUID]
) -> None:
    """State is real Postgres — a fresh service instance sees prior writes."""
    svc1 = SqlAlchemyBoardService(factory, seed["ws"])
    created = _task(svc1, seed["proj_a"], "durable")
    assert created.id is not None
    svc2 = SqlAlchemyBoardService(factory, seed["ws"])
    assert svc2.get_task(created.id).title == "durable"


# --------------------------------------------------------------------------- #
# Status workflow                                                              #
# --------------------------------------------------------------------------- #


def test_set_status_valid_transition(
    svc: SqlAlchemyBoardService, seed: dict[str, uuid.UUID]
) -> None:
    task = _task(svc, seed["proj_a"], "status")
    assert task.id is not None
    moved = svc.set_status(task.id, TaskStatus.READY)
    assert moved.status is TaskStatus.READY
    assert moved.updated_at is not None


def test_set_status_invalid_transition_raises(
    svc: SqlAlchemyBoardService, seed: dict[str, uuid.UUID]
) -> None:
    task = _task(svc, seed["proj_a"], "status")
    assert task.id is not None
    with pytest.raises(InvalidStatusTransitionError):
        svc.set_status(task.id, TaskStatus.DONE)


def test_set_status_missing_task_raises(svc: SqlAlchemyBoardService) -> None:
    with pytest.raises(EntityNotFoundError):
        svc.set_status(uuid.uuid4(), TaskStatus.READY)


# --------------------------------------------------------------------------- #
# Bulk update                                                                  #
# --------------------------------------------------------------------------- #


def test_bulk_update_applies_status_and_priority(
    svc: SqlAlchemyBoardService, seed: dict[str, uuid.UUID]
) -> None:
    t1 = _task(svc, seed["proj_a"], "b1")
    t2 = _task(svc, seed["proj_a"], "b2")
    assert t1.id is not None and t2.id is not None
    result = svc.bulk_update(
        [
            BulkUpdate(task_id=t1.id, status=TaskStatus.READY, priority=Priority.URGENT),
            BulkUpdate(task_id=t2.id, status=TaskStatus.READY),
        ]
    )
    assert {r.status for r in result} == {TaskStatus.READY}
    assert svc.get_task(t1.id).priority is Priority.URGENT


def test_bulk_update_is_atomic_on_invalid_transition(
    svc: SqlAlchemyBoardService, seed: dict[str, uuid.UUID]
) -> None:
    t1 = _task(svc, seed["proj_a"], "ok")
    t2 = _task(svc, seed["proj_a"], "bad")
    assert t1.id is not None and t2.id is not None
    with pytest.raises(InvalidStatusTransitionError):
        svc.bulk_update(
            [
                BulkUpdate(task_id=t1.id, status=TaskStatus.READY),
                BulkUpdate(task_id=t2.id, status=TaskStatus.DONE),  # illegal jump
            ]
        )
    # Nothing applied: t1 still in its original status (rolled back).
    assert svc.get_task(t1.id).status is TaskStatus.BACKLOG


def test_bulk_update_missing_task_is_atomic(
    svc: SqlAlchemyBoardService, seed: dict[str, uuid.UUID]
) -> None:
    t1 = _task(svc, seed["proj_a"], "ok")
    assert t1.id is not None
    with pytest.raises(EntityNotFoundError):
        svc.bulk_update(
            [
                BulkUpdate(task_id=t1.id, status=TaskStatus.READY),
                BulkUpdate(task_id=uuid.uuid4(), status=TaskStatus.READY),
            ]
        )
    assert svc.get_task(t1.id).status is TaskStatus.BACKLOG


# --------------------------------------------------------------------------- #
# Dependencies + cycle detection                                              #
# --------------------------------------------------------------------------- #


def test_dependency_add_records_edge(
    svc: SqlAlchemyBoardService, seed: dict[str, uuid.UUID]
) -> None:
    a = _task(svc, seed["proj_a"], "a")
    b = _task(svc, seed["proj_a"], "b")
    assert a.id is not None and b.id is not None
    svc.dependency_add(a.id, b.id)
    assert b.id in svc.get_task(a.id).depends_on


def test_dependency_add_is_idempotent(
    svc: SqlAlchemyBoardService, seed: dict[str, uuid.UUID]
) -> None:
    a = _task(svc, seed["proj_a"], "a")
    b = _task(svc, seed["proj_a"], "b")
    assert a.id is not None and b.id is not None
    svc.dependency_add(a.id, b.id)
    svc.dependency_add(a.id, b.id)  # no duplicate, no error
    assert svc.get_task(a.id).depends_on == [b.id]


def test_dependency_cycle_raises(
    svc: SqlAlchemyBoardService, seed: dict[str, uuid.UUID]
) -> None:
    a = _task(svc, seed["proj_a"], "a")
    b = _task(svc, seed["proj_a"], "b")
    assert a.id is not None and b.id is not None
    svc.dependency_add(a.id, b.id)
    with pytest.raises(CycleError):
        svc.dependency_add(b.id, a.id)


def test_transitive_dependency_cycle_raises(
    svc: SqlAlchemyBoardService, seed: dict[str, uuid.UUID]
) -> None:
    a = _task(svc, seed["proj_a"], "a")
    b = _task(svc, seed["proj_a"], "b")
    c = _task(svc, seed["proj_a"], "c")
    assert a.id is not None and b.id is not None and c.id is not None
    svc.dependency_add(a.id, b.id)
    svc.dependency_add(b.id, c.id)
    with pytest.raises(CycleError):
        svc.dependency_add(c.id, a.id)


def test_self_dependency_raises(
    svc: SqlAlchemyBoardService, seed: dict[str, uuid.UUID]
) -> None:
    a = _task(svc, seed["proj_a"], "a")
    assert a.id is not None
    with pytest.raises(CycleError):
        svc.dependency_add(a.id, a.id)


def test_dependency_add_missing_task_raises(
    svc: SqlAlchemyBoardService, seed: dict[str, uuid.UUID]
) -> None:
    a = _task(svc, seed["proj_a"], "a")
    assert a.id is not None
    with pytest.raises(EntityNotFoundError):
        svc.dependency_add(a.id, uuid.uuid4())


def test_update_task_with_dependency_cycle_raises(
    svc: SqlAlchemyBoardService, seed: dict[str, uuid.UUID]
) -> None:
    a = _task(svc, seed["proj_a"], "a")
    b = _task(svc, seed["proj_a"], "b")
    assert a.id is not None and b.id is not None
    svc.dependency_add(a.id, b.id)
    edit = svc.get_task(b.id)
    edit.depends_on = [a.id]
    with pytest.raises(CycleError):
        svc.update_task(b.id, edit)
    # The rejected edge was not persisted.
    assert svc.get_task(b.id).depends_on == []


def test_delete_task_cascades_dependency_edges(
    svc: SqlAlchemyBoardService, seed: dict[str, uuid.UUID], factory: sessionmaker[Session]
) -> None:
    a = _task(svc, seed["proj_a"], "a")
    b = _task(svc, seed["proj_a"], "b")
    assert a.id is not None and b.id is not None
    svc.dependency_add(a.id, b.id)
    svc.delete_task(a.id)
    with factory() as session:
        assert session.query(TaskDependency).count() == 0


# --------------------------------------------------------------------------- #
# Filters / saved-filter queries (filtering + ordering + pagination)          #
# --------------------------------------------------------------------------- #


def test_list_tasks_filter_by_status(
    svc: SqlAlchemyBoardService, seed: dict[str, uuid.UUID]
) -> None:
    t1 = _task(svc, seed["proj_a"], "one")
    _task(svc, seed["proj_a"], "two")
    assert t1.id is not None
    svc.set_status(t1.id, TaskStatus.READY)
    ready = svc.list_tasks(BoardFilter(statuses=[TaskStatus.READY.value]))
    assert [t.id for t in ready] == [t1.id]


def test_list_tasks_filter_by_project(
    svc: SqlAlchemyBoardService, seed: dict[str, uuid.UUID]
) -> None:
    _task(svc, seed["proj_a"], "a")
    _task(svc, seed["proj_b"], "b")
    only_b = svc.list_tasks(BoardFilter(project_id=seed["proj_b"]))
    assert len(only_b) == 1
    assert only_b[0].project_id == seed["proj_b"]


def test_list_tasks_filter_by_text(
    svc: SqlAlchemyBoardService, seed: dict[str, uuid.UUID]
) -> None:
    _task(svc, seed["proj_a"], "Implement OAuth login")
    _task(svc, seed["proj_a"], "Fix flaky test")
    hits = svc.list_tasks(BoardFilter(text="oauth"))
    assert len(hits) == 1
    assert "OAuth" in hits[0].title


def test_list_tasks_filter_by_kind_priority_and_labels(
    svc: SqlAlchemyBoardService, seed: dict[str, uuid.UUID]
) -> None:
    _task(svc, seed["proj_a"], "bug", kind=TaskKind.BUG, priority=Priority.HIGH, labels=["red"])
    _task(svc, seed["proj_a"], "feature", kind=TaskKind.FEATURE, priority=Priority.LOW)
    assert len(svc.list_tasks(BoardFilter(kinds=[TaskKind.BUG]))) == 1
    assert len(svc.list_tasks(BoardFilter(priorities=[Priority.HIGH]))) == 1
    assert len(svc.list_tasks(BoardFilter(labels=["red"]))) == 1


def test_list_tasks_ordering_is_creation_order(
    svc: SqlAlchemyBoardService, seed: dict[str, uuid.UUID]
) -> None:
    created = [_task(svc, seed["proj_a"], f"t{i}") for i in range(5)]
    listed = svc.list_tasks()
    assert [t.id for t in listed] == [t.id for t in created]


def test_list_tasks_pagination(
    svc: SqlAlchemyBoardService, seed: dict[str, uuid.UUID]
) -> None:
    created = [_task(svc, seed["proj_a"], f"t{i}") for i in range(5)]
    page = svc.list_tasks(BoardFilter(limit=2, offset=1))
    assert [t.id for t in page] == [created[1].id, created[2].id]


def test_list_tasks_no_filter_returns_all(
    svc: SqlAlchemyBoardService, seed: dict[str, uuid.UUID]
) -> None:
    _task(svc, seed["proj_a"], "a")
    _task(svc, seed["proj_a"], "b")
    assert len(svc.list_tasks()) == 2


# --------------------------------------------------------------------------- #
# Epic / Sprint / Milestone / Incident CRUD + round-trip                       #
# --------------------------------------------------------------------------- #


def test_epic_crud(svc: SqlAlchemyBoardService, seed: dict[str, uuid.UUID]) -> None:
    spec_id = uuid.uuid4()
    epic = svc.create_epic(
        EpicDTO(title="Auth epic", project_id=seed["proj_a"], labels=["auth"], spec_id=spec_id)
    )
    assert epic.id is not None
    assert epic.key == "EPIC-1"
    fetched = svc.get_epic(epic.id)
    assert fetched.title == "Auth epic"
    assert fetched.labels == ["auth"]
    assert fetched.spec_id == spec_id
    edit = fetched.model_copy(deep=True)
    edit.title = "Auth epic v2"
    updated = svc.update_epic(epic.id, edit)
    assert updated.title == "Auth epic v2"
    assert updated.key == "EPIC-1"
    assert len(svc.list_epics()) == 1
    svc.delete_epic(epic.id)
    with pytest.raises(EntityNotFoundError):
        svc.get_epic(epic.id)


def test_sprint_crud_round_trips_task_ids(
    svc: SqlAlchemyBoardService, seed: dict[str, uuid.UUID]
) -> None:
    t1 = _task(svc, seed["proj_a"], "t1")
    t2 = _task(svc, seed["proj_a"], "t2")
    assert t1.id is not None and t2.id is not None
    sprint = svc.create_sprint(
        SprintDTO(
            name="Sprint 1",
            project_id=seed["proj_a"],
            task_ids=[t1.id, t2.id],
            starts_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
    )
    assert sprint.id is not None
    fetched = svc.get_sprint(sprint.id)
    assert fetched.name == "Sprint 1"
    assert fetched.task_ids == [t1.id, t2.id]
    edit = fetched.model_copy(deep=True)
    edit.goal = "ship board"
    assert svc.update_sprint(sprint.id, edit).goal == "ship board"
    assert len(svc.list_sprints()) == 1
    svc.delete_sprint(sprint.id)
    with pytest.raises(EntityNotFoundError):
        svc.get_sprint(sprint.id)


def test_milestone_crud(svc: SqlAlchemyBoardService, seed: dict[str, uuid.UUID]) -> None:
    ms = svc.create_milestone(MilestoneDTO(name="GA", project_id=seed["proj_a"]))
    assert ms.id is not None
    assert svc.get_milestone(ms.id).name == "GA"
    edit = ms.model_copy(deep=True)
    edit.description = "general availability"
    assert svc.update_milestone(ms.id, edit).description == "general availability"
    assert len(svc.list_milestones()) == 1
    svc.delete_milestone(ms.id)
    with pytest.raises(EntityNotFoundError):
        svc.get_milestone(ms.id)


def test_incident_crud_and_key(
    svc: SqlAlchemyBoardService, seed: dict[str, uuid.UUID]
) -> None:
    inc = svc.create_incident(
        IncidentDTO(
            title="DB down",
            project_id=seed["proj_a"],
            severity=IncidentSeverity.HIGH,
            state=IncidentState.ALERT_RECEIVED,
        )
    )
    assert inc.id is not None
    assert inc.key == "INC-1"
    fetched = svc.get_incident(inc.id)
    assert fetched.title == "DB down"
    assert fetched.severity is IncidentSeverity.HIGH
    edit = fetched.model_copy(deep=True)
    edit.title = "DB degraded"
    edit.state = IncidentState.CONTEXT_GATHERING
    updated = svc.update_incident(inc.id, edit)
    assert updated.title == "DB degraded"
    assert updated.state is IncidentState.CONTEXT_GATHERING
    # A second incident with an unset dedup_key is allowed (partial index exempts NULL).
    inc2 = svc.create_incident(IncidentDTO(title="Second", project_id=seed["proj_a"]))
    assert inc2.key == "INC-2"
    assert len(svc.list_incidents()) == 2
    svc.delete_incident(inc.id)
    with pytest.raises(EntityNotFoundError):
        svc.get_incident(inc.id)


def test_list_epics_filter_by_project(
    svc: SqlAlchemyBoardService, seed: dict[str, uuid.UUID]
) -> None:
    svc.create_epic(EpicDTO(title="a", project_id=seed["proj_a"]))
    svc.create_epic(EpicDTO(title="b", project_id=seed["proj_b"]))
    assert len(svc.list_epics(BoardFilter(project_id=seed["proj_a"]))) == 1


# --------------------------------------------------------------------------- #
# Constraints + cross-workspace isolation                                      #
# --------------------------------------------------------------------------- #


def test_cross_workspace_isolation(
    factory: sessionmaker[Session], seed: dict[str, uuid.UUID]
) -> None:
    svc_a = SqlAlchemyBoardService(factory, seed["ws"])
    svc_other = SqlAlchemyBoardService(factory, seed["other_ws"])
    mine = _task(svc_a, seed["proj_a"], "mine")
    assert mine.id is not None
    # The other workspace cannot see, fetch, or mutate it (a foreign id is 404).
    with pytest.raises(EntityNotFoundError):
        svc_other.get_task(mine.id)
    assert svc_other.list_tasks() == []
    # Keys are per-workspace: the other workspace starts its own TASK-1 sequence.
    theirs = _task(svc_other, seed["other_proj"], "theirs")
    assert theirs.key == "TASK-1"


def test_key_unique_per_workspace(
    factory: sessionmaker[Session], seed: dict[str, uuid.UUID]
) -> None:
    """The (workspace_id, key) unique constraint is enforced by Postgres."""
    with factory() as session:
        session.add(
            Task(workspace_id=seed["ws"], project_id=seed["proj_a"], key="TASK-1", title="x")
        )
        session.commit()
        session.add(
            Task(workspace_id=seed["ws"], project_id=seed["proj_a"], key="TASK-1", title="y")
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
