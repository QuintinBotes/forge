"""Observability + audit router stubs (filled by Task 1.14 — observability).

Immutable audit log + step-level run-trace assembly for the trace viewer.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends

from forge_api._stubs import NotImplementedResponse, eventual, not_implemented
from forge_api.deps import CurrentPrincipal, get_current_principal
from forge_contracts import MCPAuditEntry, Step

router = APIRouter(
    prefix="/observability",
    tags=["observability"],
    dependencies=[Depends(get_current_principal)],
    responses={501: {"model": NotImplementedResponse}},
)

_R = "observability"


@router.get(
    "/audit",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(MCPAuditEntry, "Query the immutable audit log (redacted)."),
)
def list_audit(principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "list_audit")


@router.get(
    "/runs/{run_id}/trace",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(Step, "Assemble a step-level run trace."),
)
def get_run_trace(run_id: uuid.UUID, principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "get_run_trace")
