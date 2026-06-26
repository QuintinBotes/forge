"""Agent runtime router stubs (filled by Task 1.9 — agent-runtime)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends

from forge_api._stubs import NotImplementedResponse, eventual, not_implemented
from forge_api.deps import CurrentPrincipal, get_current_principal
from forge_contracts import AgentRunResult

router = APIRouter(
    prefix="/agent",
    tags=["agent"],
    dependencies=[Depends(get_current_principal)],
    responses={501: {"model": NotImplementedResponse}},
)

_R = "agent"


@router.post(
    "/runs",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(AgentRunResult, "Run an agent objective (plan->act->observe)."),
)
def run(principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "run")


@router.get(
    "/runs/{run_id}",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(AgentRunResult, "Fetch an agent run result with its steps."),
)
def get_run(run_id: uuid.UUID, principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "get_run")
