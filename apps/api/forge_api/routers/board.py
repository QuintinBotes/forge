"""Board router (Task 1.5 — board-core).

Serves the ``BoardService`` surface over HTTP: CRUD for Epic / Task / Sprint /
Milestone / Incident, plus the cross-cutting status, bulk-update and dependency
operations. Handlers delegate to a process-wide :class:`InMemoryBoardService`
(Phase 1: hermetic, no Postgres). The DB-backed service is swapped in behind the
same dependency at the Phase-2 wire-up barrier via ``app.dependency_overrides``.

Domain errors map to HTTP: missing entity -> 404; dependency cycle or illegal
status transition -> 409 Conflict.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

from forge_api.auth.rbac import Permission
from forge_api.db import get_session_factory
from forge_api.deps import Principal, get_current_principal
from forge_api.routers._rbac import require_permission
from forge_api.settings import get_settings
from forge_board import InMemoryBoardService, SqlAlchemyBoardService
from forge_board.exceptions import (
    CycleError,
    EntityNotFoundError,
    InvalidStatusTransitionError,
)
from forge_contracts import (
    BoardFilter,
    BoardService,
    BulkUpdate,
    EpicDTO,
    IncidentDTO,
    MilestoneDTO,
    Priority,
    SprintDTO,
    TaskDTO,
    TaskKind,
    TaskStatus,
)

router = APIRouter(
    prefix="/board",
    tags=["board"],
    dependencies=[Depends(get_current_principal)],
)

# RBAC (Phase-2 bug fix r3): the board is the primary write surface, so every
# mutating route must *authorize*, not just authenticate. The router-level
# ``get_current_principal`` handles auth (401); ``_REQUIRE_WRITE`` is attached
# per-route to the create/update/delete/status/bulk/dependency handlers so a
# read-only ``viewer`` (and the ``agent-runner``, which lacks WRITE) gets 403.
# Reads stay open to any authenticated role (all roles hold READ).
_REQUIRE_WRITE = Depends(require_permission(Permission.WRITE))


# --------------------------------------------------------------------------- #
# Per-workspace service registry (tenant isolation)                            #
# --------------------------------------------------------------------------- #


class BoardServiceRegistry:
    """Vends a :class:`~forge_contracts.BoardService` per workspace (tenant isolation).

    Phase-2 bug fix r4: the board is the primary write surface and must enforce
    the spec's mandatory per-workspace isolation (design doc section 4; plan Task
    1.15) like every other tenant surface hardened in r3. The frozen
    ``BoardService`` protocol has no ``workspace_id`` dimension, and the entities
    (``TaskDTO`` etc.) carry no workspace field, so isolation is achieved by
    giving each workspace its **own** service instance. A caller can therefore
    only ever see, fetch, mutate, or delete entities in its own workspace; a
    foreign id simply does not exist there (404), and listing never spans tenants.

    The backend is chosen once (``FORGE_BOARD_BACKEND``): ``memory`` (default)
    vends a hermetic :class:`InMemoryBoardService`; ``db`` vends a
    :class:`SqlAlchemyBoardService` bound to the workspace + shared session
    factory, which filters by ``workspace_id`` behind the same dependency. Both
    satisfy the same frozen ``BoardService`` protocol, so the router is agnostic.
    """

    def __init__(
        self,
        *,
        backend: str = "memory",
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        self._backend = backend
        self._session_factory = session_factory
        self._services: dict[uuid.UUID, BoardService] = {}

    def for_workspace(self, workspace_id: uuid.UUID) -> BoardService:
        service = self._services.get(workspace_id)
        if service is None:
            if self._backend == "db":
                if self._session_factory is None:  # pragma: no cover - misconfiguration
                    raise RuntimeError("db board backend requires a session factory")
                service = SqlAlchemyBoardService(self._session_factory, workspace_id)
            else:
                service = InMemoryBoardService()
            self._services[workspace_id] = service
        return service


@lru_cache(maxsize=1)
def _board_registry_singleton() -> BoardServiceRegistry:
    if get_settings().board_backend == "db":
        return BoardServiceRegistry(backend="db", session_factory=get_session_factory())
    return BoardServiceRegistry(backend="memory")


def get_board_registry() -> BoardServiceRegistry:
    """Return the process-wide board registry (override in tests via DI)."""
    return _board_registry_singleton()


def get_board_service(
    principal: Annotated[Principal, Depends(get_current_principal)],
    registry: Annotated[BoardServiceRegistry, Depends(get_board_registry)],
) -> BoardService:
    """Return the board service scoped to the caller's workspace.

    Scoping happens here (not in the handlers) so every route — reads and writes
    alike — operates only on the authenticated workspace's entities. Tests may
    override this dependency directly to inject a single shared service.
    """
    return registry.for_workspace(principal.workspace_id)


BoardServiceDep = Annotated[BoardService, Depends(get_board_service)]


# --------------------------------------------------------------------------- #
# Error mapping + request bodies                                              #
# --------------------------------------------------------------------------- #


@contextmanager
def _domain_errors() -> Iterator[None]:
    """Translate board domain exceptions into HTTP error responses."""
    try:
        yield
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (CycleError, InvalidStatusTransitionError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


class StatusUpdateRequest(BaseModel):
    """Body for ``POST /board/tasks/{task_id}/status``."""

    status: TaskStatus


class DependencyRequest(BaseModel):
    """Body for ``POST /board/tasks/{task_id}/dependencies``."""

    depends_on_id: uuid.UUID


def _build_task_filter(
    *,
    project_id: uuid.UUID | None,
    statuses: list[str] | None,
    kinds: list[TaskKind] | None,
    priorities: list[Priority] | None,
    labels: list[str] | None,
    assignee_id: uuid.UUID | None,
    sprint_id: uuid.UUID | None,
    epic_id: uuid.UUID | None,
    text: str | None,
    limit: int | None,
    offset: int,
) -> BoardFilter:
    return BoardFilter(
        project_id=project_id,
        statuses=statuses or [],
        kinds=kinds or [],
        priorities=priorities or [],
        labels=labels or [],
        assignee_id=assignee_id,
        sprint_id=sprint_id,
        epic_id=epic_id,
        text=text,
        limit=limit,
        offset=offset,
    )


# --------------------------------------------------------------------------- #
# Tasks                                                                        #
# --------------------------------------------------------------------------- #


@router.get("/tasks", response_model=list[TaskDTO])
def list_tasks(
    svc: BoardServiceDep,
    project_id: uuid.UUID | None = None,
    status: Annotated[list[str] | None, Query()] = None,
    kind: Annotated[list[TaskKind] | None, Query()] = None,
    priority: Annotated[list[Priority] | None, Query()] = None,
    label: Annotated[list[str] | None, Query()] = None,
    assignee_id: uuid.UUID | None = None,
    sprint_id: uuid.UUID | None = None,
    epic_id: uuid.UUID | None = None,
    text: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[TaskDTO]:
    return svc.list_tasks(
        _build_task_filter(
            project_id=project_id,
            statuses=status,
            kinds=kind,
            priorities=priority,
            labels=label,
            assignee_id=assignee_id,
            sprint_id=sprint_id,
            epic_id=epic_id,
            text=text,
            limit=limit,
            offset=offset,
        )
    )


@router.post(
    "/tasks",
    response_model=TaskDTO,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_REQUIRE_WRITE],
)
def create_task(svc: BoardServiceDep, data: TaskDTO) -> TaskDTO:
    return svc.create_task(data)


@router.post("/tasks/bulk", response_model=list[TaskDTO], dependencies=[_REQUIRE_WRITE])
def bulk_update(svc: BoardServiceDep, updates: list[BulkUpdate]) -> list[TaskDTO]:
    with _domain_errors():
        return svc.bulk_update(updates)


@router.get("/tasks/{task_id}", response_model=TaskDTO)
def get_task(svc: BoardServiceDep, task_id: uuid.UUID) -> TaskDTO:
    with _domain_errors():
        return svc.get_task(task_id)


@router.patch("/tasks/{task_id}", response_model=TaskDTO, dependencies=[_REQUIRE_WRITE])
def update_task(svc: BoardServiceDep, task_id: uuid.UUID, data: TaskDTO) -> TaskDTO:
    with _domain_errors():
        return svc.update_task(task_id, data)


@router.delete(
    "/tasks/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[_REQUIRE_WRITE],
)
def delete_task(svc: BoardServiceDep, task_id: uuid.UUID) -> None:
    with _domain_errors():
        svc.delete_task(task_id)


@router.post("/tasks/{task_id}/status", response_model=TaskDTO, dependencies=[_REQUIRE_WRITE])
def set_task_status(
    svc: BoardServiceDep, task_id: uuid.UUID, payload: StatusUpdateRequest
) -> TaskDTO:
    with _domain_errors():
        return svc.set_status(task_id, payload.status)


@router.post(
    "/tasks/{task_id}/dependencies",
    response_model=TaskDTO,
    dependencies=[_REQUIRE_WRITE],
)
def add_dependency(
    svc: BoardServiceDep, task_id: uuid.UUID, payload: DependencyRequest
) -> TaskDTO:
    with _domain_errors():
        svc.dependency_add(task_id, payload.depends_on_id)
        return svc.get_task(task_id)


# --------------------------------------------------------------------------- #
# Epics                                                                        #
# --------------------------------------------------------------------------- #


@router.get("/epics", response_model=list[EpicDTO])
def list_epics(
    svc: BoardServiceDep,
    project_id: uuid.UUID | None = None,
    text: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[EpicDTO]:
    return svc.list_epics(
        BoardFilter(project_id=project_id, text=text, limit=limit, offset=offset)
    )


@router.post(
    "/epics",
    response_model=EpicDTO,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_REQUIRE_WRITE],
)
def create_epic(svc: BoardServiceDep, data: EpicDTO) -> EpicDTO:
    return svc.create_epic(data)


@router.get("/epics/{epic_id}", response_model=EpicDTO)
def get_epic(svc: BoardServiceDep, epic_id: uuid.UUID) -> EpicDTO:
    with _domain_errors():
        return svc.get_epic(epic_id)


@router.patch("/epics/{epic_id}", response_model=EpicDTO, dependencies=[_REQUIRE_WRITE])
def update_epic(svc: BoardServiceDep, epic_id: uuid.UUID, data: EpicDTO) -> EpicDTO:
    with _domain_errors():
        return svc.update_epic(epic_id, data)


@router.delete(
    "/epics/{epic_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[_REQUIRE_WRITE],
)
def delete_epic(svc: BoardServiceDep, epic_id: uuid.UUID) -> None:
    with _domain_errors():
        svc.delete_epic(epic_id)


# --------------------------------------------------------------------------- #
# Sprints                                                                      #
# --------------------------------------------------------------------------- #


@router.get("/sprints", response_model=list[SprintDTO])
def list_sprints(
    svc: BoardServiceDep,
    project_id: uuid.UUID | None = None,
    text: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[SprintDTO]:
    return svc.list_sprints(
        BoardFilter(project_id=project_id, text=text, limit=limit, offset=offset)
    )


@router.post(
    "/sprints",
    response_model=SprintDTO,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_REQUIRE_WRITE],
)
def create_sprint(svc: BoardServiceDep, data: SprintDTO) -> SprintDTO:
    return svc.create_sprint(data)


@router.get("/sprints/{sprint_id}", response_model=SprintDTO)
def get_sprint(svc: BoardServiceDep, sprint_id: uuid.UUID) -> SprintDTO:
    with _domain_errors():
        return svc.get_sprint(sprint_id)


@router.patch("/sprints/{sprint_id}", response_model=SprintDTO, dependencies=[_REQUIRE_WRITE])
def update_sprint(svc: BoardServiceDep, sprint_id: uuid.UUID, data: SprintDTO) -> SprintDTO:
    with _domain_errors():
        return svc.update_sprint(sprint_id, data)


@router.delete(
    "/sprints/{sprint_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[_REQUIRE_WRITE],
)
def delete_sprint(svc: BoardServiceDep, sprint_id: uuid.UUID) -> None:
    with _domain_errors():
        svc.delete_sprint(sprint_id)


# --------------------------------------------------------------------------- #
# Milestones                                                                   #
# --------------------------------------------------------------------------- #


@router.get("/milestones", response_model=list[MilestoneDTO])
def list_milestones(
    svc: BoardServiceDep,
    project_id: uuid.UUID | None = None,
    text: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[MilestoneDTO]:
    return svc.list_milestones(
        BoardFilter(project_id=project_id, text=text, limit=limit, offset=offset)
    )


@router.post(
    "/milestones",
    response_model=MilestoneDTO,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_REQUIRE_WRITE],
)
def create_milestone(svc: BoardServiceDep, data: MilestoneDTO) -> MilestoneDTO:
    return svc.create_milestone(data)


@router.get("/milestones/{milestone_id}", response_model=MilestoneDTO)
def get_milestone(svc: BoardServiceDep, milestone_id: uuid.UUID) -> MilestoneDTO:
    with _domain_errors():
        return svc.get_milestone(milestone_id)


@router.patch(
    "/milestones/{milestone_id}",
    response_model=MilestoneDTO,
    dependencies=[_REQUIRE_WRITE],
)
def update_milestone(
    svc: BoardServiceDep, milestone_id: uuid.UUID, data: MilestoneDTO
) -> MilestoneDTO:
    with _domain_errors():
        return svc.update_milestone(milestone_id, data)


@router.delete(
    "/milestones/{milestone_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[_REQUIRE_WRITE],
)
def delete_milestone(svc: BoardServiceDep, milestone_id: uuid.UUID) -> None:
    with _domain_errors():
        svc.delete_milestone(milestone_id)


# --------------------------------------------------------------------------- #
# Incidents                                                                    #
# --------------------------------------------------------------------------- #


@router.get("/incidents", response_model=list[IncidentDTO])
def list_incidents(
    svc: BoardServiceDep,
    project_id: uuid.UUID | None = None,
    text: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[IncidentDTO]:
    return svc.list_incidents(
        BoardFilter(project_id=project_id, text=text, limit=limit, offset=offset)
    )


@router.post(
    "/incidents",
    response_model=IncidentDTO,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_REQUIRE_WRITE],
)
def create_incident(svc: BoardServiceDep, data: IncidentDTO) -> IncidentDTO:
    return svc.create_incident(data)


@router.get("/incidents/{incident_id}", response_model=IncidentDTO)
def get_incident(svc: BoardServiceDep, incident_id: uuid.UUID) -> IncidentDTO:
    with _domain_errors():
        return svc.get_incident(incident_id)


@router.patch(
    "/incidents/{incident_id}",
    response_model=IncidentDTO,
    dependencies=[_REQUIRE_WRITE],
)
def update_incident(
    svc: BoardServiceDep, incident_id: uuid.UUID, data: IncidentDTO
) -> IncidentDTO:
    with _domain_errors():
        return svc.update_incident(incident_id, data)


@router.delete(
    "/incidents/{incident_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[_REQUIRE_WRITE],
)
def delete_incident(svc: BoardServiceDep, incident_id: uuid.UUID) -> None:
    with _domain_errors():
        svc.delete_incident(incident_id)
