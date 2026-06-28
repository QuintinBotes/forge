"""Sprint lifecycle + velocity router (F26).

Serves the DB-backed :class:`forge_board.sprint_service.SprintService` over HTTP:
create/edit/start/complete/cancel/recompute sprints, plus burndown / report /
velocity-dashboard reads. All routes auth-required; ``workspace_id`` is resolved
from the principal; reads need READ (viewer+), mutations need WRITE (member+;
``viewer`` and ``agent-runner`` lack WRITE -> 403). Cross-workspace ids are 404
(no existence leak).

Error mapping mirrors F01's envelope: ``active_sprint_exists`` / ``sprint_state``
-> 409, invalid body -> 422, missing/foreign id -> 404.
"""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from forge_api.auth.rbac import Permission
from forge_api.db import get_session_factory
from forge_api.deps import Principal, get_current_principal
from forge_api.routers._rbac import require_permission
from forge_api.schemas.sprint import (
    BurndownSeriesView,
    CompleteSprintRequest,
    RecomputeResponse,
    SprintCreate,
    SprintReportView,
    SprintUpdate,
    SprintView,
    VelocityDashboardView,
)
from forge_board.exceptions import ActiveSprintExistsError, SprintStateError
from forge_board.sprint_service import (
    InvalidSprintRequest,
    SprintNotFound,
    SprintService,
)
from forge_contracts.enums import SprintState

router = APIRouter(tags=["sprints"], dependencies=[Depends(get_current_principal)])

ReaderDep = Annotated[Principal, Depends(require_permission(Permission.READ))]
WriterDep = Annotated[Principal, Depends(require_permission(Permission.WRITE))]


@lru_cache(maxsize=1)
def _service_singleton() -> SprintService:
    return SprintService(session_factory=get_session_factory())


def get_sprint_service() -> SprintService:
    """Return the process-wide sprint service (override in tests via DI)."""
    return _service_singleton()


ServiceDep = Annotated[SprintService, Depends(get_sprint_service)]


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except ActiveSprintExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "active_sprint_exists", "sprint_id": str(exc.sprint_id)},
        ) from exc
    except SprintStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "sprint_state", "from": str(exc.frm), "to": str(exc.to)},
        ) from exc
    except InvalidSprintRequest as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except SprintNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


# --------------------------------------------------------------------------- #
# Project-scoped CRUD + list                                                   #
# --------------------------------------------------------------------------- #


@router.post(
    "/projects/{project_id}/sprints",
    response_model=SprintView,
    status_code=status.HTTP_201_CREATED,
)
def create_sprint(
    service: ServiceDep, principal: WriterDep, project_id: uuid.UUID, body: SprintCreate
) -> SprintView:
    with _errors():
        return service.create(
            workspace_id=principal.workspace_id,
            project_id=project_id,
            name=body.name,
            goal=body.goal,
            start_date=body.start_date,
            end_date=body.end_date,
            capacity_points=body.capacity_points,
        )


@router.get("/projects/{project_id}/sprints", response_model=list[SprintView])
def list_sprints(
    service: ServiceDep,
    principal: ReaderDep,
    project_id: uuid.UUID,
    state: SprintState | None = None,
    limit: Annotated[int, Query(ge=1, le=250)] = 50,
) -> list[SprintView]:
    return service.list_sprints(
        workspace_id=principal.workspace_id,
        project_id=project_id,
        state=state,
        limit=limit,
    )


@router.get("/projects/{project_id}/velocity", response_model=VelocityDashboardView)
def velocity_dashboard(
    service: ServiceDep,
    principal: ReaderDep,
    project_id: uuid.UUID,
    last: Annotated[int, Query(ge=1, le=26)] = 6,
) -> VelocityDashboardView:
    return service.velocity_dashboard(
        workspace_id=principal.workspace_id, project_id=project_id, last=last
    )


@router.get("/projects/{project_id}/velocity/export")
def velocity_export(
    service: ServiceDep,
    principal: ReaderDep,
    project_id: uuid.UUID,
    fmt: Annotated[Literal["csv", "json"], Query(alias="format")] = "csv",
) -> Response:
    rows = service.velocity_export_rows(workspace_id=principal.workspace_id, project_id=project_id)
    if fmt == "json":
        import json

        return Response(content=json.dumps(rows), media_type="application/json")
    buf = io.StringIO()
    fields = [
        "sprint_id",
        "name",
        "end_date",
        "committed_points",
        "completed_points",
        "predictability",
    ]
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return Response(content=buf.getvalue(), media_type="text/csv")


# --------------------------------------------------------------------------- #
# Sprint-scoped operations                                                     #
# --------------------------------------------------------------------------- #


@router.get("/sprints/{sprint_id}", response_model=SprintView)
def get_sprint(service: ServiceDep, principal: ReaderDep, sprint_id: uuid.UUID) -> SprintView:
    with _errors():
        return service.get(workspace_id=principal.workspace_id, sprint_id=sprint_id)


@router.patch("/sprints/{sprint_id}", response_model=SprintView)
def update_sprint(
    service: ServiceDep, principal: WriterDep, sprint_id: uuid.UUID, body: SprintUpdate
) -> SprintView:
    with _errors():
        return service.update(
            workspace_id=principal.workspace_id,
            sprint_id=sprint_id,
            name=body.name,
            goal=body.goal,
            start_date=body.start_date,
            end_date=body.end_date,
            capacity_points=body.capacity_points,
        )


@router.post("/sprints/{sprint_id}/start", response_model=SprintView)
def start_sprint(service: ServiceDep, principal: WriterDep, sprint_id: uuid.UUID) -> SprintView:
    with _errors():
        return service.start(
            workspace_id=principal.workspace_id,
            sprint_id=sprint_id,
            actor_id=principal.user_id,
        )


@router.post("/sprints/{sprint_id}/complete", response_model=SprintReportView)
def complete_sprint(
    service: ServiceDep,
    principal: WriterDep,
    sprint_id: uuid.UUID,
    body: CompleteSprintRequest,
) -> SprintReportView:
    with _errors():
        return service.complete(
            workspace_id=principal.workspace_id,
            sprint_id=sprint_id,
            carryover=body.carryover,
            next_sprint_id=body.next_sprint_id,
            actor_id=principal.user_id,
        )


@router.post("/sprints/{sprint_id}/cancel", response_model=SprintView)
def cancel_sprint(service: ServiceDep, principal: WriterDep, sprint_id: uuid.UUID) -> SprintView:
    with _errors():
        return service.cancel(
            workspace_id=principal.workspace_id,
            sprint_id=sprint_id,
            actor_id=principal.user_id,
        )


@router.post("/sprints/{sprint_id}/recompute", response_model=RecomputeResponse)
def recompute_sprint(
    service: ServiceDep, principal: WriterDep, sprint_id: uuid.UUID
) -> RecomputeResponse:
    with _errors():
        service.reconcile(sprint_id=sprint_id, workspace_id=principal.workspace_id)
        view = service.get(workspace_id=principal.workspace_id, sprint_id=sprint_id)
        return RecomputeResponse(enqueued=True, velocity_version=view.velocity_version)


@router.get("/sprints/{sprint_id}/burndown", response_model=BurndownSeriesView)
def sprint_burndown(
    service: ServiceDep,
    principal: ReaderDep,
    sprint_id: uuid.UUID,
    as_of: Annotated[str | None, Query()] = None,
) -> BurndownSeriesView:
    parsed = None
    if as_of:
        from datetime import date as _date

        try:
            parsed = _date.fromisoformat(as_of)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid as_of"
            ) from exc
    with _errors():
        return service.burndown(
            workspace_id=principal.workspace_id, sprint_id=sprint_id, as_of=parsed
        )


@router.get("/sprints/{sprint_id}/report", response_model=SprintReportView)
def sprint_report(
    service: ServiceDep, principal: ReaderDep, sprint_id: uuid.UUID
) -> SprintReportView:
    with _errors():
        return service.report(workspace_id=principal.workspace_id, sprint_id=sprint_id)


__all__ = ["get_sprint_service", "router"]
