"""Approval router stubs (the human-in-the-loop gate surface).

Backed by the approval data model; decision handling is wired alongside the
workflow engine (Task 1.8) and observability (Task 1.14).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends

from forge_api._stubs import NotImplementedResponse, eventual, not_implemented
from forge_api.deps import CurrentPrincipal, get_current_principal
from forge_contracts import ApprovalRequest

router = APIRouter(
    prefix="/approval",
    tags=["approval"],
    dependencies=[Depends(get_current_principal)],
    responses={501: {"model": NotImplementedResponse}},
)

_R = "approval"


@router.get(
    "/requests",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(ApprovalRequest, "List pending approval requests."),
)
def list_requests(principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "list_requests")


@router.get(
    "/requests/{approval_id}",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(ApprovalRequest, "Fetch one approval request (full context)."),
)
def get_request(approval_id: uuid.UUID, principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "get_request")


@router.post(
    "/requests/{approval_id}/decision",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(ApprovalRequest, "Approve / reject / request changes."),
)
def decide(approval_id: uuid.UUID, principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "decide")
