"""Workflow engine router stubs (filled by Task 1.8 — workflow-engine)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends

from forge_api._stubs import NotImplementedResponse, eventual, not_implemented
from forge_api.deps import CurrentPrincipal, get_current_principal
from forge_contracts import WorkflowRun

router = APIRouter(
    prefix="/workflow",
    tags=["workflow"],
    dependencies=[Depends(get_current_principal)],
    responses={501: {"model": NotImplementedResponse}},
)

_R = "workflow"


@router.post(
    "/runs",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(WorkflowRun, "Start a workflow run for a task."),
)
def start_run(principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "start")


@router.get(
    "/runs/{run_id}",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(WorkflowRun, "Fetch a workflow run."),
)
def get_run(run_id: uuid.UUID, principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "get_run")


@router.post(
    "/runs/{run_id}/transition",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(WorkflowRun, "Apply an FSM transition event."),
)
def transition(run_id: uuid.UUID, principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "transition")
