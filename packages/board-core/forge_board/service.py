"""In-memory :class:`~forge_contracts.BoardService` implementation (plan Task 1.5).

This is the board domain layer: CRUD for Epic/Task/Sprint/Milestone/Incident, the
default status workflow, bulk operations, saved-filter queries, and a dependency
graph with cycle detection. It is intentionally storage-agnostic and hermetic —
state lives in process memory so the unit suite runs without Postgres (Phase-1
rule: unit tests are isolated). A Postgres-backed implementation can be swapped
in at the Phase-2 wire-up barrier behind the same frozen protocol.

Identity model: ids and human-facing keys (``TASK-1``, ``EPIC-1``, ``INC-1``) are
server-assigned on create; ``key`` and ``created_at`` are immutable across
updates. Returned DTOs are always deep copies, so callers cannot mutate stored
state by side effect.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel

from forge_board.exceptions import EntityNotFoundError
from forge_board.graph import has_cycle, would_create_cycle
from forge_board.workflow import validate_transition
from forge_contracts import (
    BoardFilter,
    BulkUpdate,
    CycleError,
    EpicDTO,
    IncidentDTO,
    MilestoneDTO,
    SprintDTO,
    TaskDTO,
    TaskStatus,
)


def _now() -> datetime:
    return datetime.now(UTC)


class _Collection[T: BaseModel]:
    """A typed in-memory table keyed by UUID, with monotonic key generation."""

    def __init__(self, entity: str, key_prefix: str | None = None) -> None:
        self.entity = entity
        self.key_prefix = key_prefix
        self._items: dict[uuid.UUID, T] = {}
        self._key_counter = 0

    def raw(self, entity_id: uuid.UUID) -> T:
        """Return the live stored object (internal mutation), or raise."""
        try:
            return self._items[entity_id]
        except KeyError:
            raise EntityNotFoundError(self.entity, entity_id) from None

    def get(self, entity_id: uuid.UUID) -> T:
        """Return an isolated deep copy of a stored object, or raise."""
        return self.raw(entity_id).model_copy(deep=True)

    def put(self, entity_id: uuid.UUID, item: T) -> None:
        self._items[entity_id] = item

    def delete(self, entity_id: uuid.UUID) -> None:
        self.raw(entity_id)
        del self._items[entity_id]

    def list_all(self) -> list[T]:
        return list(self._items.values())

    def next_key(self) -> str:
        self._key_counter += 1
        return f"{self.key_prefix}-{self._key_counter}"


def _paginate[T: BaseModel](items: list[T], f: BoardFilter | None) -> list[T]:
    if f is None:
        return items
    start = f.offset or 0
    if f.limit is not None:
        return items[start : start + f.limit]
    return items[start:]


class InMemoryBoardService:
    """A complete, hermetic board domain service (implements ``BoardService``)."""

    def __init__(self) -> None:
        self._epics: _Collection[EpicDTO] = _Collection("epic", "EPIC")
        self._tasks: _Collection[TaskDTO] = _Collection("task", "TASK")
        self._sprints: _Collection[SprintDTO] = _Collection("sprint")
        self._milestones: _Collection[MilestoneDTO] = _Collection("milestone")
        self._incidents: _Collection[IncidentDTO] = _Collection("incident", "INC")

    # ------------------------------------------------------------------ #
    # Generic create / update builders                                    #
    # ------------------------------------------------------------------ #

    def _new_item[T: BaseModel](self, coll: _Collection[T], data: T) -> T:
        """Build + store a fresh entity: assign id, key, and timestamps."""
        new_id = uuid.uuid4()
        fields = type(data).model_fields
        update: dict[str, object] = {"id": new_id}
        if coll.key_prefix is not None and "key" in fields:
            update["key"] = coll.next_key()
        now = _now()
        if "created_at" in fields:
            update["created_at"] = now
        if "updated_at" in fields:
            update["updated_at"] = now
        item = data.model_copy(update=update, deep=True)
        coll.put(new_id, item)
        return item

    def _merged_item[T: BaseModel](self, coll: _Collection[T], entity_id: uuid.UUID, data: T) -> T:
        """Build (not store) a full-replace update preserving id/key/created_at."""
        existing = coll.raw(entity_id).model_dump()
        fields = type(data).model_fields
        update: dict[str, object] = {"id": entity_id}
        if "key" in fields and existing.get("key") is not None:
            update["key"] = existing["key"]
        if "created_at" in fields:
            update["created_at"] = existing.get("created_at")
        if "updated_at" in fields:
            update["updated_at"] = _now()
        return data.model_copy(update=update, deep=True)

    # ------------------------------------------------------------------ #
    # Epic                                                                #
    # ------------------------------------------------------------------ #

    def create_epic(self, data: EpicDTO) -> EpicDTO:
        return self._new_item(self._epics, data).model_copy(deep=True)

    def get_epic(self, epic_id: uuid.UUID) -> EpicDTO:
        return self._epics.get(epic_id)

    def update_epic(self, epic_id: uuid.UUID, data: EpicDTO) -> EpicDTO:
        item = self._merged_item(self._epics, epic_id, data)
        self._epics.put(epic_id, item)
        return item.model_copy(deep=True)

    def list_epics(self, filter: BoardFilter | None = None) -> list[EpicDTO]:
        items = [e for e in self._epics.list_all() if _epic_matches(e, filter)]
        return [e.model_copy(deep=True) for e in _paginate(items, filter)]

    def delete_epic(self, epic_id: uuid.UUID) -> None:
        self._epics.delete(epic_id)

    # ------------------------------------------------------------------ #
    # Task                                                                #
    # ------------------------------------------------------------------ #

    def create_task(self, data: TaskDTO) -> TaskDTO:
        return self._new_item(self._tasks, data).model_copy(deep=True)

    def get_task(self, task_id: uuid.UUID) -> TaskDTO:
        return self._tasks.get(task_id)

    def update_task(self, task_id: uuid.UUID, data: TaskDTO) -> TaskDTO:
        item = self._merged_item(self._tasks, task_id, data)
        edges = self._task_edges()
        edges[task_id] = set(item.depends_on)
        if has_cycle(edges):
            raise CycleError(f"updating task {task_id} dependencies would create a cycle")
        self._tasks.put(task_id, item)
        return item.model_copy(deep=True)

    def list_tasks(self, filter: BoardFilter | None = None) -> list[TaskDTO]:
        items = [t for t in self._tasks.list_all() if _task_matches(t, filter)]
        return [t.model_copy(deep=True) for t in _paginate(items, filter)]

    def delete_task(self, task_id: uuid.UUID) -> None:
        self._tasks.delete(task_id)

    # ------------------------------------------------------------------ #
    # Sprint                                                              #
    # ------------------------------------------------------------------ #

    def create_sprint(self, data: SprintDTO) -> SprintDTO:
        return self._new_item(self._sprints, data).model_copy(deep=True)

    def get_sprint(self, sprint_id: uuid.UUID) -> SprintDTO:
        return self._sprints.get(sprint_id)

    def update_sprint(self, sprint_id: uuid.UUID, data: SprintDTO) -> SprintDTO:
        item = self._merged_item(self._sprints, sprint_id, data)
        self._sprints.put(sprint_id, item)
        return item.model_copy(deep=True)

    def list_sprints(self, filter: BoardFilter | None = None) -> list[SprintDTO]:
        items = [
            s
            for s in self._sprints.list_all()
            if _simple_matches(s.project_id, [s.name, s.goal], filter)
        ]
        return [s.model_copy(deep=True) for s in _paginate(items, filter)]

    def delete_sprint(self, sprint_id: uuid.UUID) -> None:
        self._sprints.delete(sprint_id)

    # ------------------------------------------------------------------ #
    # Milestone                                                           #
    # ------------------------------------------------------------------ #

    def create_milestone(self, data: MilestoneDTO) -> MilestoneDTO:
        return self._new_item(self._milestones, data).model_copy(deep=True)

    def get_milestone(self, milestone_id: uuid.UUID) -> MilestoneDTO:
        return self._milestones.get(milestone_id)

    def update_milestone(self, milestone_id: uuid.UUID, data: MilestoneDTO) -> MilestoneDTO:
        item = self._merged_item(self._milestones, milestone_id, data)
        self._milestones.put(milestone_id, item)
        return item.model_copy(deep=True)

    def list_milestones(self, filter: BoardFilter | None = None) -> list[MilestoneDTO]:
        items = [
            m
            for m in self._milestones.list_all()
            if _simple_matches(m.project_id, [m.name, m.description], filter)
        ]
        return [m.model_copy(deep=True) for m in _paginate(items, filter)]

    def delete_milestone(self, milestone_id: uuid.UUID) -> None:
        self._milestones.delete(milestone_id)

    # ------------------------------------------------------------------ #
    # Incident                                                            #
    # ------------------------------------------------------------------ #

    def create_incident(self, data: IncidentDTO) -> IncidentDTO:
        return self._new_item(self._incidents, data).model_copy(deep=True)

    def get_incident(self, incident_id: uuid.UUID) -> IncidentDTO:
        return self._incidents.get(incident_id)

    def update_incident(self, incident_id: uuid.UUID, data: IncidentDTO) -> IncidentDTO:
        item = self._merged_item(self._incidents, incident_id, data)
        self._incidents.put(incident_id, item)
        return item.model_copy(deep=True)

    def list_incidents(self, filter: BoardFilter | None = None) -> list[IncidentDTO]:
        items = [
            i
            for i in self._incidents.list_all()
            if _simple_matches(i.project_id, [i.title, i.description], filter)
        ]
        return [i.model_copy(deep=True) for i in _paginate(items, filter)]

    def delete_incident(self, incident_id: uuid.UUID) -> None:
        self._incidents.delete(incident_id)

    # ------------------------------------------------------------------ #
    # Cross-cutting: status, bulk, dependencies                           #
    # ------------------------------------------------------------------ #

    def set_status(self, task_id: uuid.UUID, status: TaskStatus) -> TaskDTO:
        task = self._tasks.raw(task_id)
        validate_transition(task.status, status)
        task.status = status
        task.updated_at = _now()
        return task.model_copy(deep=True)

    def bulk_update(self, updates: list[BulkUpdate]) -> list[TaskDTO]:
        # Two-pass for atomicity: validate everything before mutating anything.
        planned: list[tuple[BulkUpdate, TaskDTO]] = []
        for update in updates:
            task = self._tasks.raw(update.task_id)
            if update.status is not None:
                validate_transition(task.status, update.status)
            planned.append((update, task))

        now = _now()
        results: list[TaskDTO] = []
        for update, task in planned:
            if update.status is not None:
                task.status = update.status
            if update.priority is not None:
                task.priority = update.priority
            if update.assignee_id is not None:
                task.assignee_id = update.assignee_id
            if update.sprint_id is not None:
                task.sprint_id = update.sprint_id
            if update.labels is not None:
                task.labels = update.labels
            task.updated_at = now
            results.append(task.model_copy(deep=True))
        return results

    def dependency_add(self, task_id: uuid.UUID, depends_on_id: uuid.UUID) -> None:
        self._tasks.raw(task_id)
        self._tasks.raw(depends_on_id)
        if would_create_cycle(self._task_edges(), task_id, depends_on_id):
            raise CycleError(f"task {task_id} depending on {depends_on_id} would create a cycle")
        task = self._tasks.raw(task_id)
        if depends_on_id not in task.depends_on:
            task.depends_on = [*task.depends_on, depends_on_id]
            task.updated_at = _now()

    # ------------------------------------------------------------------ #
    # Internals                                                           #
    # ------------------------------------------------------------------ #

    def _task_edges(self) -> dict[uuid.UUID, set[uuid.UUID]]:
        """Build the dependency adjacency (``task -> its depends_on``)."""
        edges: dict[uuid.UUID, set[uuid.UUID]] = {}
        for task in self._tasks.list_all():
            if task.id is not None:
                edges[task.id] = set(task.depends_on)
        return edges


# --------------------------------------------------------------------------- #
# Filter predicates                                                            #
# --------------------------------------------------------------------------- #


def _text_match(needle: str | None, haystack: list[str | None]) -> bool:
    if not needle:
        return True
    hay = " ".join(part for part in haystack if part).lower()
    return needle.lower() in hay


def _task_matches(task: TaskDTO, f: BoardFilter | None) -> bool:
    if f is None:
        return True
    if f.project_id is not None and task.project_id != f.project_id:
        return False
    if f.statuses and task.status.value not in f.statuses:
        return False
    if f.kinds and task.kind not in f.kinds:
        return False
    if f.priorities and task.priority not in f.priorities:
        return False
    if f.labels and not (set(f.labels) & set(task.labels)):
        return False
    if f.assignee_id is not None and task.assignee_id != f.assignee_id:
        return False
    if f.sprint_id is not None and task.sprint_id != f.sprint_id:
        return False
    if f.epic_id is not None and task.epic_id != f.epic_id:
        return False
    return _text_match(f.text, [task.title, task.description])


def _epic_matches(epic: EpicDTO, f: BoardFilter | None) -> bool:
    if f is None:
        return True
    if f.project_id is not None and epic.project_id != f.project_id:
        return False
    if f.statuses and epic.status not in f.statuses:
        return False
    if f.labels and not (set(f.labels) & set(epic.labels)):
        return False
    if f.epic_id is not None and epic.id != f.epic_id:
        return False
    return _text_match(f.text, [epic.title, epic.description])


def _simple_matches(
    project_id: uuid.UUID | None, text_fields: list[str | None], f: BoardFilter | None
) -> bool:
    if f is None:
        return True
    if f.project_id is not None and project_id != f.project_id:
        return False
    return _text_match(f.text, text_fields)


__all__ = ["InMemoryBoardService"]
