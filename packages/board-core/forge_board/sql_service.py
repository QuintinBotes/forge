"""Postgres-backed :class:`~forge_contracts.BoardService` (F01 board persistence).

A drop-in, workspace-scoped alternative to :class:`InMemoryBoardService` that
persists the *same* board DTOs (Epic / Task / Sprint / Milestone / Incident, the
dependency graph, the status workflow, bulk ops, and saved-filter queries) to
real Postgres via ``forge_db``. It implements the same frozen ``BoardService``
protocol, so the API/service factory swaps it in behind ``FORGE_BOARD_BACKEND=db``
with no behavioural change — the default stays ``memory`` and the in-memory store
remains the unit-test default.

Scoping mirrors the router's per-workspace ``BoardServiceRegistry``: one instance
is bound to a single ``workspace_id`` and only ever sees/mutates entities in that
workspace (a foreign id is a 404 — ``EntityNotFoundError`` — never a leak). The
``BoardService`` protocol carries no ``workspace_id`` dimension, so it is supplied
at construction, exactly as the registry vends a per-workspace in-memory service.

Filtering + pagination reuse the in-memory store's own predicates over freshly
rebuilt DTOs, so saved-filter semantics are byte-identical across both backends.

Fidelity notes vs the in-memory store (both satisfy the same protocol):

* ``key`` keeps the ``TASK-N`` / ``EPIC-N`` / ``INC-N`` shape, allocated as
  ``max(existing suffix) + 1`` within the workspace (the ``(workspace_id, key)``
  unique constraint guards collisions).
* ``knowledge_scope`` / ``handoff_rules`` round-trip ``None`` faithfully: a DTO
  ``None`` is stored as JSON ``null`` and read back as ``None``, distinct from an
  all-defaults empty object.
* Referential integrity is real: a task's ``project_id`` must reference an
  existing project and a dependency edge must reference existing tasks. This is
  the one storage-boundary divergence from the hermetic in-memory store (which
  holds free-floating DTOs); ``start_date`` / ``end_date`` / ``due_date`` land in
  the pre-existing naive ``timestamp`` columns, so aware inputs are normalised to
  naive UTC.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import delete, select

from forge_board.exceptions import EntityNotFoundError
from forge_board.graph import has_cycle, would_create_cycle
from forge_board.service import (
    _epic_matches,
    _paginate,
    _simple_matches,
    _task_matches,
)
from forge_board.workflow import validate_transition
from forge_contracts import (
    AcceptanceCriterion,
    ApprovalPolicy,
    BoardFilter,
    BulkUpdate,
    CycleError,
    EpicDTO,
    HandoffRules,
    IncidentDTO,
    KnowledgeScope,
    MilestoneDTO,
    RepoTarget,
    SprintDTO,
    SubAgentPolicy,
    TaskDTO,
    TaskStatus,
)
from forge_contracts import enums as ce
from forge_db.models import Epic, Incident, Milestone, Sprint, Task, TaskDependency
from forge_db.models import enums as dbe

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker


def _now() -> datetime:
    return datetime.now(UTC)


def _ev(value: object) -> str:
    """Return an enum member's ``.value`` (or the value itself if already a str)."""
    return value.value if hasattr(value, "value") else str(value)


def _naive(dt: datetime | None) -> datetime | None:
    """Normalise an (optionally aware) datetime to naive UTC for a naive column."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def _dedup(ids: list[uuid.UUID]) -> list[uuid.UUID]:
    """Order-preserving de-duplication (the adjacency table is a set of edges)."""
    seen: set[uuid.UUID] = set()
    out: list[uuid.UUID] = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


class SqlAlchemyBoardService:
    """A Postgres-backed board domain service (implements ``BoardService``)."""

    def __init__(self, session_factory: sessionmaker[Session], workspace_id: uuid.UUID) -> None:
        self._sf = session_factory
        self._ws = workspace_id

    # ------------------------------------------------------------------ #
    # Shared internals                                                    #
    # ------------------------------------------------------------------ #

    def _get(self, session: Session, model: type, entity_id: uuid.UUID, entity: str):
        row = session.get(model, entity_id)
        if row is None or row.workspace_id != self._ws:
            raise EntityNotFoundError(entity, entity_id)
        return row

    def _next_key(self, session: Session, model: type, prefix: str) -> str:
        keys = (
            session.execute(
                select(model.key).where(
                    model.workspace_id == self._ws, model.key.like(f"{prefix}-%")
                )
            )
            .scalars()
            .all()
        )
        max_n = 0
        for key in keys:
            suffix = str(key).rsplit("-", 1)[-1]
            if suffix.isdigit():
                max_n = max(max_n, int(suffix))
        return f"{prefix}-{max_n + 1}"

    def _task_edges(self, session: Session) -> dict[uuid.UUID, set[uuid.UUID]]:
        edges: dict[uuid.UUID, set[uuid.UUID]] = {}
        for row in (
            session.execute(select(TaskDependency).where(TaskDependency.workspace_id == self._ws))
            .scalars()
            .all()
        ):
            edges.setdefault(row.task_id, set()).add(row.depends_on_id)
        return edges

    def _depends_on(self, session: Session, task_id: uuid.UUID) -> list[uuid.UUID]:
        rows = (
            session.execute(
                select(TaskDependency).where(
                    TaskDependency.workspace_id == self._ws,
                    TaskDependency.task_id == task_id,
                )
            )
            .scalars()
            .all()
        )
        rows.sort(key=lambda r: (r.created_at, r.id))
        return [r.depends_on_id for r in rows]

    def _set_edges(self, session: Session, task_id: uuid.UUID, depends_on: list[uuid.UUID]) -> None:
        session.execute(
            delete(TaskDependency).where(
                TaskDependency.workspace_id == self._ws,
                TaskDependency.task_id == task_id,
            )
        )
        session.flush()
        for dep in _dedup(depends_on):
            session.add(
                TaskDependency(
                    workspace_id=self._ws,
                    task_id=task_id,
                    depends_on_id=dep,
                    created_at=_now(),
                )
            )
        session.flush()

    # ------------------------------------------------------------------ #
    # Epic                                                                #
    # ------------------------------------------------------------------ #

    def _epic_to_dto(self, row: Epic) -> EpicDTO:
        return EpicDTO(
            id=row.id,
            key=row.key,
            project_id=row.project_id,
            title=row.title,
            description=row.description,
            status=row.status,
            spec_id=row.spec_id,
            labels=list(row.labels or []),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def create_epic(self, data: EpicDTO) -> EpicDTO:
        with self._sf() as session:
            now = _now()
            row = Epic(
                id=uuid.uuid4(),
                workspace_id=self._ws,
                project_id=data.project_id,
                key=self._next_key(session, Epic, "EPIC"),
                title=data.title,
                description=data.description,
                status=data.status,
                spec_id=data.spec_id,
                labels=list(data.labels),
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.commit()
            return self._epic_to_dto(row)

    def get_epic(self, epic_id: uuid.UUID) -> EpicDTO:
        with self._sf() as session:
            return self._epic_to_dto(self._get(session, Epic, epic_id, "epic"))

    def update_epic(self, epic_id: uuid.UUID, data: EpicDTO) -> EpicDTO:
        with self._sf() as session:
            row = self._get(session, Epic, epic_id, "epic")
            row.project_id = data.project_id
            row.title = data.title
            row.description = data.description
            row.status = data.status
            row.spec_id = data.spec_id
            row.labels = list(data.labels)
            row.updated_at = _now()
            session.commit()
            return self._epic_to_dto(row)

    def list_epics(self, filter: BoardFilter | None = None) -> list[EpicDTO]:
        with self._sf() as session:
            rows = (
                session.execute(
                    select(Epic)
                    .where(Epic.workspace_id == self._ws)
                    .order_by(Epic.created_at, Epic.id)
                )
                .scalars()
                .all()
            )
            dtos = [self._epic_to_dto(r) for r in rows]
            return _paginate([e for e in dtos if _epic_matches(e, filter)], filter)

    def delete_epic(self, epic_id: uuid.UUID) -> None:
        with self._sf() as session:
            session.delete(self._get(session, Epic, epic_id, "epic"))
            session.commit()

    # ------------------------------------------------------------------ #
    # Task                                                                #
    # ------------------------------------------------------------------ #

    def _apply_task_fields(self, row: Task, data: TaskDTO) -> None:
        row.project_id = data.project_id
        row.epic_id = data.epic_id
        row.spec_id = data.spec_id
        row.sprint_id = data.sprint_id
        row.milestone_id = data.milestone_id
        row.assignee_id = data.assignee_id
        row.kind = dbe.TaskKind(data.kind.value)
        row.title = data.title
        row.description = data.description
        row.status = dbe.TaskStatus(data.status.value)
        row.priority = dbe.Priority(data.priority.value)
        row.estimate = data.estimate
        row.execution_mode = dbe.ExecutionMode(data.execution_mode.value)
        row.repo_targets = [m.model_dump(mode="json") for m in data.repo_targets]
        row.instructions_profile = data.instructions_profile
        row.skill_profile = data.skill_profile
        row.allowed_actions = list(data.allowed_actions)
        row.restricted_actions = list(data.restricted_actions)
        row.requires_approval = data.requires_approval.model_dump(mode="json")
        row.knowledge_scope = (
            data.knowledge_scope.model_dump(mode="json")
            if data.knowledge_scope is not None
            else None
        )
        row.subagent_policy = data.subagent_policy.model_dump(mode="json")
        row.handoff_rules = (
            data.handoff_rules.model_dump(mode="json") if data.handoff_rules is not None else None
        )
        row.acceptance_criteria = [m.model_dump(mode="json") for m in data.acceptance_criteria]
        row.labels = list(data.labels)

    def _task_to_dto(self, session: Session, row: Task) -> TaskDTO:
        return TaskDTO(
            id=row.id,
            key=row.key,
            project_id=row.project_id,
            epic_id=row.epic_id,
            spec_id=row.spec_id,
            kind=ce.TaskKind(_ev(row.kind)),
            title=row.title,
            description=row.description,
            status=ce.TaskStatus(_ev(row.status)),
            priority=ce.Priority(_ev(row.priority)),
            estimate=row.estimate,
            execution_mode=ce.ExecutionMode(_ev(row.execution_mode)),
            repo_targets=[RepoTarget.model_validate(x) for x in (row.repo_targets or [])],
            instructions_profile=row.instructions_profile,
            skill_profile=row.skill_profile,
            acceptance_criteria=[
                AcceptanceCriterion.model_validate(x) for x in (row.acceptance_criteria or [])
            ],
            allowed_actions=list(row.allowed_actions or []),
            restricted_actions=list(row.restricted_actions or []),
            requires_approval=ApprovalPolicy.model_validate(row.requires_approval or {}),
            knowledge_scope=(
                KnowledgeScope.model_validate(row.knowledge_scope)
                if row.knowledge_scope is not None
                else None
            ),
            subagent_policy=SubAgentPolicy.model_validate(row.subagent_policy or {}),
            handoff_rules=(
                HandoffRules.model_validate(row.handoff_rules)
                if row.handoff_rules is not None
                else None
            ),
            labels=list(row.labels or []),
            assignee_id=row.assignee_id,
            sprint_id=row.sprint_id,
            milestone_id=row.milestone_id,
            depends_on=self._depends_on(session, row.id),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def create_task(self, data: TaskDTO) -> TaskDTO:
        with self._sf() as session:
            now = _now()
            new_id = uuid.uuid4()
            row = Task(
                id=new_id,
                workspace_id=self._ws,
                key=self._next_key(session, Task, "TASK"),
                created_at=now,
                updated_at=now,
            )
            self._apply_task_fields(row, data)
            session.add(row)
            session.flush()
            self._set_edges(session, new_id, data.depends_on)
            session.commit()
            return self._task_to_dto(session, row)

    def get_task(self, task_id: uuid.UUID) -> TaskDTO:
        with self._sf() as session:
            return self._task_to_dto(session, self._get(session, Task, task_id, "task"))

    def update_task(self, task_id: uuid.UUID, data: TaskDTO) -> TaskDTO:
        with self._sf() as session:
            row = self._get(session, Task, task_id, "task")
            edges = self._task_edges(session)
            edges[task_id] = set(data.depends_on)
            if has_cycle(edges):
                raise CycleError(f"updating task {task_id} dependencies would create a cycle")
            self._apply_task_fields(row, data)
            row.updated_at = _now()
            self._set_edges(session, task_id, data.depends_on)
            session.commit()
            return self._task_to_dto(session, row)

    def list_tasks(self, filter: BoardFilter | None = None) -> list[TaskDTO]:
        with self._sf() as session:
            rows = (
                session.execute(
                    select(Task)
                    .where(Task.workspace_id == self._ws)
                    .order_by(Task.created_at, Task.id)
                )
                .scalars()
                .all()
            )
            dtos = [self._task_to_dto(session, r) for r in rows]
            return _paginate([t for t in dtos if _task_matches(t, filter)], filter)

    def delete_task(self, task_id: uuid.UUID) -> None:
        with self._sf() as session:
            row = self._get(session, Task, task_id, "task")
            session.execute(
                delete(TaskDependency).where(
                    TaskDependency.workspace_id == self._ws,
                    (TaskDependency.task_id == task_id) | (TaskDependency.depends_on_id == task_id),
                )
            )
            session.delete(row)
            session.commit()

    # ------------------------------------------------------------------ #
    # Sprint                                                              #
    # ------------------------------------------------------------------ #

    def _sprint_to_dto(self, row: Sprint) -> SprintDTO:
        return SprintDTO(
            id=row.id,
            project_id=row.project_id,
            name=row.name,
            goal=row.goal,
            starts_at=row.start_date,
            ends_at=row.end_date,
            task_ids=[uuid.UUID(str(t)) for t in (row.task_ids or [])],
        )

    def create_sprint(self, data: SprintDTO) -> SprintDTO:
        with self._sf() as session:
            now = _now()
            row = Sprint(
                id=uuid.uuid4(),
                workspace_id=self._ws,
                project_id=data.project_id,
                name=data.name,
                goal=data.goal,
                start_date=_naive(data.starts_at),
                end_date=_naive(data.ends_at),
                task_ids=[str(t) for t in data.task_ids],
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.commit()
            return self._sprint_to_dto(row)

    def get_sprint(self, sprint_id: uuid.UUID) -> SprintDTO:
        with self._sf() as session:
            return self._sprint_to_dto(self._get(session, Sprint, sprint_id, "sprint"))

    def update_sprint(self, sprint_id: uuid.UUID, data: SprintDTO) -> SprintDTO:
        with self._sf() as session:
            row = self._get(session, Sprint, sprint_id, "sprint")
            row.project_id = data.project_id
            row.name = data.name
            row.goal = data.goal
            row.start_date = _naive(data.starts_at)
            row.end_date = _naive(data.ends_at)
            row.task_ids = [str(t) for t in data.task_ids]
            row.updated_at = _now()
            session.commit()
            return self._sprint_to_dto(row)

    def list_sprints(self, filter: BoardFilter | None = None) -> list[SprintDTO]:
        with self._sf() as session:
            rows = (
                session.execute(
                    select(Sprint)
                    .where(Sprint.workspace_id == self._ws)
                    .order_by(Sprint.created_at, Sprint.id)
                )
                .scalars()
                .all()
            )
            dtos = [self._sprint_to_dto(r) for r in rows]
            return _paginate(
                [s for s in dtos if _simple_matches(s.project_id, [s.name, s.goal], filter)],
                filter,
            )

    def delete_sprint(self, sprint_id: uuid.UUID) -> None:
        with self._sf() as session:
            session.delete(self._get(session, Sprint, sprint_id, "sprint"))
            session.commit()

    # ------------------------------------------------------------------ #
    # Milestone                                                           #
    # ------------------------------------------------------------------ #

    def _milestone_to_dto(self, row: Milestone) -> MilestoneDTO:
        return MilestoneDTO(
            id=row.id,
            project_id=row.project_id,
            name=row.name,
            description=row.description,
            due_at=row.due_date,
        )

    def create_milestone(self, data: MilestoneDTO) -> MilestoneDTO:
        with self._sf() as session:
            now = _now()
            row = Milestone(
                id=uuid.uuid4(),
                workspace_id=self._ws,
                project_id=data.project_id,
                name=data.name,
                description=data.description,
                due_date=_naive(data.due_at),
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.commit()
            return self._milestone_to_dto(row)

    def get_milestone(self, milestone_id: uuid.UUID) -> MilestoneDTO:
        with self._sf() as session:
            return self._milestone_to_dto(self._get(session, Milestone, milestone_id, "milestone"))

    def update_milestone(self, milestone_id: uuid.UUID, data: MilestoneDTO) -> MilestoneDTO:
        with self._sf() as session:
            row = self._get(session, Milestone, milestone_id, "milestone")
            row.project_id = data.project_id
            row.name = data.name
            row.description = data.description
            row.due_date = _naive(data.due_at)
            row.updated_at = _now()
            session.commit()
            return self._milestone_to_dto(row)

    def list_milestones(self, filter: BoardFilter | None = None) -> list[MilestoneDTO]:
        with self._sf() as session:
            rows = (
                session.execute(
                    select(Milestone)
                    .where(Milestone.workspace_id == self._ws)
                    .order_by(Milestone.created_at, Milestone.id)
                )
                .scalars()
                .all()
            )
            dtos = [self._milestone_to_dto(r) for r in rows]
            return _paginate(
                [m for m in dtos if _simple_matches(m.project_id, [m.name, m.description], filter)],
                filter,
            )

    def delete_milestone(self, milestone_id: uuid.UUID) -> None:
        with self._sf() as session:
            session.delete(self._get(session, Milestone, milestone_id, "milestone"))
            session.commit()

    # ------------------------------------------------------------------ #
    # Incident                                                            #
    # ------------------------------------------------------------------ #

    def _incident_to_dto(self, row: Incident) -> IncidentDTO:
        return IncidentDTO(
            id=row.id,
            key=row.key,
            project_id=row.project_id,
            title=row.title,
            description=row.description,
            severity=ce.IncidentSeverity(_ev(row.severity)),
            state=ce.IncidentState(_ev(row.state)),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def create_incident(self, data: IncidentDTO) -> IncidentDTO:
        with self._sf() as session:
            now = _now()
            row = Incident(
                id=uuid.uuid4(),
                workspace_id=self._ws,
                project_id=data.project_id,
                key=self._next_key(session, Incident, "INC"),
                title=data.title,
                description=data.description,
                severity=dbe.IncidentSeverity(data.severity.value),
                state=dbe.IncidentState(data.state.value),
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.commit()
            return self._incident_to_dto(row)

    def get_incident(self, incident_id: uuid.UUID) -> IncidentDTO:
        with self._sf() as session:
            return self._incident_to_dto(self._get(session, Incident, incident_id, "incident"))

    def update_incident(self, incident_id: uuid.UUID, data: IncidentDTO) -> IncidentDTO:
        with self._sf() as session:
            row = self._get(session, Incident, incident_id, "incident")
            row.project_id = data.project_id
            row.title = data.title
            row.description = data.description
            row.severity = dbe.IncidentSeverity(data.severity.value)
            row.state = dbe.IncidentState(data.state.value)
            row.updated_at = _now()
            session.commit()
            return self._incident_to_dto(row)

    def list_incidents(self, filter: BoardFilter | None = None) -> list[IncidentDTO]:
        with self._sf() as session:
            rows = (
                session.execute(
                    select(Incident)
                    .where(Incident.workspace_id == self._ws)
                    .order_by(Incident.created_at, Incident.id)
                )
                .scalars()
                .all()
            )
            dtos = [self._incident_to_dto(r) for r in rows]
            return _paginate(
                [
                    i
                    for i in dtos
                    if _simple_matches(i.project_id, [i.title, i.description], filter)
                ],
                filter,
            )

    def delete_incident(self, incident_id: uuid.UUID) -> None:
        with self._sf() as session:
            session.delete(self._get(session, Incident, incident_id, "incident"))
            session.commit()

    # ------------------------------------------------------------------ #
    # Cross-cutting: status, bulk, dependencies                           #
    # ------------------------------------------------------------------ #

    def set_status(self, task_id: uuid.UUID, status: TaskStatus) -> TaskDTO:
        with self._sf() as session:
            row = self._get(session, Task, task_id, "task")
            validate_transition(ce.TaskStatus(_ev(row.status)), status)
            row.status = dbe.TaskStatus(status.value)
            row.updated_at = _now()
            session.commit()
            return self._task_to_dto(session, row)

    def bulk_update(self, updates: list[BulkUpdate]) -> list[TaskDTO]:
        with self._sf() as session:
            # Two-pass for atomicity: validate everything before mutating anything.
            planned: list[tuple[BulkUpdate, Task]] = []
            for update in updates:
                row = self._get(session, Task, update.task_id, "task")
                if update.status is not None:
                    validate_transition(ce.TaskStatus(_ev(row.status)), update.status)
                planned.append((update, row))

            now = _now()
            for update, row in planned:
                if update.status is not None:
                    row.status = dbe.TaskStatus(update.status.value)
                if update.priority is not None:
                    row.priority = dbe.Priority(update.priority.value)
                if update.assignee_id is not None:
                    row.assignee_id = update.assignee_id
                if update.sprint_id is not None:
                    row.sprint_id = update.sprint_id
                if update.labels is not None:
                    row.labels = list(update.labels)
                row.updated_at = now
            session.flush()
            results = [self._task_to_dto(session, row) for _u, row in planned]
            session.commit()
            return results

    def dependency_add(self, task_id: uuid.UUID, depends_on_id: uuid.UUID) -> None:
        with self._sf() as session:
            self._get(session, Task, task_id, "task")
            self._get(session, Task, depends_on_id, "task")
            if would_create_cycle(self._task_edges(session), task_id, depends_on_id):
                raise CycleError(
                    f"task {task_id} depending on {depends_on_id} would create a cycle"
                )
            existing = session.execute(
                select(TaskDependency).where(
                    TaskDependency.workspace_id == self._ws,
                    TaskDependency.task_id == task_id,
                    TaskDependency.depends_on_id == depends_on_id,
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    TaskDependency(
                        workspace_id=self._ws,
                        task_id=task_id,
                        depends_on_id=depends_on_id,
                        created_at=_now(),
                    )
                )
                task = session.get(Task, task_id)
                task.updated_at = _now()
                session.commit()


__all__ = ["SqlAlchemyBoardService"]
