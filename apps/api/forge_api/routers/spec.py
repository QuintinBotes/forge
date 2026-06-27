"""Spec engine router stubs (filled by Task 1.7 — spec-engine).

Covers the SDD lifecycle: constitution -> spec_create -> clarify -> plan ->
tasks -> validate, plus manifest read/write.

# PARKED: HTTP wiring of these routes to ``forge_spec.FileSpecEngine`` is
# deferred to Phase 2 Task 2.1 ("All routers serve real handlers"). Filling the
# handlers in Phase 1 would break the Phase-0 invariant asserted by
# ``apps/api/tests/test_api_skeleton.py::test_all_stub_routes_return_501`` (and
# POST routes that gain a required body would return 422, not 501) — that test
# is owned by the API-skeleton task, not Task 1.7. The engine itself is fully
# implemented and tested at the package level (``packages/spec-engine``); the
# Phase-2 integration step swaps these bodies for calls into the engine and
# relaxes the skeleton's all-501 assertion in one coherent change.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends

from forge_api._stubs import NotImplementedResponse, eventual, not_implemented
from forge_api.deps import CurrentPrincipal, get_current_principal
from forge_contracts import (
    Constitution,
    SpecManifest,
    TaskDTO,
    ValidationReport,
)

router = APIRouter(
    prefix="/spec",
    tags=["spec"],
    dependencies=[Depends(get_current_principal)],
    responses={501: {"model": NotImplementedResponse}},
)

_R = "spec"


@router.post(
    "/constitution",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(Constitution, "Initialise a project constitution."),
)
def constitution_init(principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "constitution_init")


@router.post(
    "/specs",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(SpecManifest, "Create a spec for an epic."),
)
def spec_create(principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "spec_create")


@router.get(
    "/specs/{spec_id}",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(SpecManifest, "Read a spec manifest."),
)
def read_manifest(spec_id: uuid.UUID, principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "read_manifest")


@router.put(
    "/specs/{spec_id}",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(SpecManifest, "Write a spec manifest."),
)
def write_manifest(spec_id: uuid.UUID, principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "write_manifest")


@router.post(
    "/specs/{spec_id}/clarify",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(SpecManifest, "Run the clarification pass."),
)
def spec_clarify(spec_id: uuid.UUID, principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "spec_clarify")


@router.post(
    "/specs/{spec_id}/plan",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(SpecManifest, "Generate the technical plan."),
)
def spec_plan(spec_id: uuid.UUID, principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "spec_plan")


@router.post(
    "/specs/{spec_id}/tasks",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(TaskDTO, "Generate tasks from an approved spec."),
)
def spec_tasks(spec_id: uuid.UUID, principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "spec_tasks")


@router.post(
    "/tasks/{task_id}/validate",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(ValidationReport, "Validate a task against its spec."),
)
def validate(task_id: uuid.UUID, principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "validate")
