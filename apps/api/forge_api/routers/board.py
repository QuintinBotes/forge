"""Board router stubs (filled by Task 1.5 — board-core).

Endpoints cover Epic / Task / Sprint / Milestone / Incident CRUD plus the
cross-cutting status, bulk-update and dependency operations of ``BoardService``.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends

from forge_api._stubs import NotImplementedResponse, eventual, not_implemented
from forge_api.deps import CurrentPrincipal, get_current_principal
from forge_contracts import (
    EpicDTO,
    IncidentDTO,
    MilestoneDTO,
    SprintDTO,
    TaskDTO,
)

router = APIRouter(
    prefix="/board",
    tags=["board"],
    dependencies=[Depends(get_current_principal)],
    responses={501: {"model": NotImplementedResponse}},
)

_R = "board"


# --- Tasks --------------------------------------------------------------- #


@router.get(
    "/tasks",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(TaskDTO, "List tasks matching a board filter."),
)
def list_tasks(principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "list_tasks")


@router.post(
    "/tasks",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(TaskDTO, "Create a task."),
)
def create_task(principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "create_task")


@router.get(
    "/tasks/{task_id}",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(TaskDTO, "Fetch a single task."),
)
def get_task(task_id: uuid.UUID, principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "get_task")


@router.patch(
    "/tasks/{task_id}",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(TaskDTO, "Update a task."),
)
def update_task(task_id: uuid.UUID, principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "update_task")


@router.delete(
    "/tasks/{task_id}",
    response_model=NotImplementedResponse,
    status_code=501,
)
def delete_task(task_id: uuid.UUID, principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "delete_task")


@router.post(
    "/tasks/{task_id}/status",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(TaskDTO, "Transition a task's status."),
)
def set_task_status(task_id: uuid.UUID, principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "set_status")


@router.post(
    "/tasks/bulk",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(TaskDTO, "Bulk-update a set of tasks."),
)
def bulk_update(principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "bulk_update")


@router.post(
    "/tasks/{task_id}/dependencies",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(TaskDTO, "Add a dependency edge (rejects cycles)."),
)
def add_dependency(task_id: uuid.UUID, principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "dependency_add")


# --- Epics --------------------------------------------------------------- #


@router.get(
    "/epics",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(EpicDTO, "List epics."),
)
def list_epics(principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "list_epics")


@router.post(
    "/epics",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(EpicDTO, "Create an epic."),
)
def create_epic(principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "create_epic")


# --- Sprints / Milestones / Incidents ------------------------------------ #


@router.get(
    "/sprints",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(SprintDTO, "List sprints."),
)
def list_sprints(principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "list_sprints")


@router.get(
    "/milestones",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(MilestoneDTO, "List milestones."),
)
def list_milestones(principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "list_milestones")


@router.get(
    "/incidents",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(IncidentDTO, "List incidents."),
)
def list_incidents(principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "list_incidents")
