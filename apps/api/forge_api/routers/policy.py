"""Policy SDK router stubs (filled by Task 1.10 — policy-sdk)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from forge_api._stubs import NotImplementedResponse, eventual, not_implemented
from forge_api.deps import CurrentPrincipal, get_current_principal
from forge_contracts import Decision, Policy

router = APIRouter(
    prefix="/policy",
    tags=["policy"],
    dependencies=[Depends(get_current_principal)],
    responses={501: {"model": NotImplementedResponse}},
)

_R = "policy"


@router.get(
    "",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(Policy, "Load the effective repo policy."),
)
def load(principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "load")


@router.post(
    "/evaluate",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(Decision, "Evaluate a tool call against the policy."),
)
def evaluate(principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "evaluate")
