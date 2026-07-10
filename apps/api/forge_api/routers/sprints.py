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
    CapacityReportResponse,
    CFDResponse,
    CompleteSprintRequest,
    CycleLeadTimeResponse,
    EstimateChange,
    EstimationScaleCreate,
    EstimationScaleUpdate,
    EstimationScaleView,
    GoalAlignmentResponse,
    MemberCapacityUpdate,
    PortfolioVelocityResponse,
    RecomputeResponse,
    SprintCreate,
    SprintReportView,
    SprintUpdate,
    SprintView,
    VelocityDashboardView,
)
from forge_api.services.automations import ApiAutomationDispatcher
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
    return SprintService(
        session_factory=get_session_factory(), dispatcher=ApiAutomationDispatcher()
    )


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
            calendar_weekend_days=body.calendar_weekend_days,
            calendar_holidays=body.calendar_holidays,
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
            calendar_weekend_days=body.calendar_weekend_days,
            calendar_holidays=body.calendar_holidays,
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


@router.get("/tasks/{task_id}/estimate-history", response_model=list[EstimateChange])
def task_estimate_history(
    service: ServiceDep, principal: ReaderDep, task_id: uuid.UUID
) -> list[EstimateChange]:
    with _errors():
        return service.estimate_history(workspace_id=principal.workspace_id, task_id=task_id)


# --------------------------------------------------------------------------- #
# F40 PM depth: per-member capacity, sprint-goal alignment, portfolio rollups  #
# --------------------------------------------------------------------------- #


@router.post(
    "/estimation-scales", response_model=EstimationScaleView, status_code=status.HTTP_201_CREATED
)
def create_estimation_scale(
    service: ServiceDep, principal: WriterDep, body: EstimationScaleCreate
) -> EstimationScaleView:
    with _errors():
        return service.create_estimation_scale(
            workspace_id=principal.workspace_id,
            project_id=body.project_id,
            name=body.name,
            unit=body.unit,
            values=body.values,
            is_default=body.is_default,
        )


@router.get("/estimation-scales", response_model=list[EstimationScaleView])
def list_estimation_scales(
    service: ServiceDep,
    principal: ReaderDep,
    project_id: uuid.UUID | None = None,
) -> list[EstimationScaleView]:
    return service.list_estimation_scales(
        workspace_id=principal.workspace_id, project_id=project_id
    )


@router.patch("/estimation-scales/{scale_id}", response_model=EstimationScaleView)
def update_estimation_scale(
    service: ServiceDep,
    principal: WriterDep,
    scale_id: uuid.UUID,
    body: EstimationScaleUpdate,
) -> EstimationScaleView:
    with _errors():
        return service.update_estimation_scale(
            workspace_id=principal.workspace_id,
            scale_id=scale_id,
            name=body.name,
            unit=body.unit,
            values=body.values,
            is_default=body.is_default,
        )


@router.get("/sprints/{sprint_id}/capacity", response_model=CapacityReportResponse)
def sprint_capacity(
    service: ServiceDep, principal: ReaderDep, sprint_id: uuid.UUID
) -> CapacityReportResponse:
    with _errors():
        members = service.capacity_report(workspace_id=principal.workspace_id, sprint_id=sprint_id)
        return CapacityReportResponse(sprint_id=sprint_id, members=members)


@router.put("/sprints/{sprint_id}/capacity", status_code=status.HTTP_204_NO_CONTENT)
def set_sprint_member_capacity(
    service: ServiceDep, principal: WriterDep, sprint_id: uuid.UUID, body: MemberCapacityUpdate
) -> None:
    with _errors():
        service.set_member_capacity(
            workspace_id=principal.workspace_id,
            sprint_id=sprint_id,
            member_id=body.member_id,
            capacity_points=body.capacity_points,
        )


@router.get("/sprints/{sprint_id}/goal-alignment", response_model=GoalAlignmentResponse)
def sprint_goal_alignment(
    service: ServiceDep, principal: ReaderDep, sprint_id: uuid.UUID
) -> GoalAlignmentResponse:
    with _errors():
        result = service.goal_alignment(workspace_id=principal.workspace_id, sprint_id=sprint_id)
        return GoalAlignmentResponse(sprint_id=sprint_id, **result.model_dump())


@router.get("/projects/{project_id}/cfd", response_model=CFDResponse)
def project_cfd(
    service: ServiceDep,
    principal: ReaderDep,
    project_id: uuid.UUID,
    start: Annotated[str, Query()],
    end: Annotated[str, Query()],
) -> CFDResponse:
    from datetime import date as _date

    try:
        start_d, end_d = _date.fromisoformat(start), _date.fromisoformat(end)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid start/end"
        ) from exc
    points = service.cfd(
        workspace_id=principal.workspace_id, project_id=project_id, start=start_d, end=end_d
    )
    return CFDResponse(project_id=project_id, start=start_d, end=end_d, points=points)


@router.get("/projects/{project_id}/cycle-lead-time", response_model=CycleLeadTimeResponse)
def project_cycle_lead_time(
    service: ServiceDep, principal: ReaderDep, project_id: uuid.UUID
) -> CycleLeadTimeResponse:
    tasks, avg_lead, avg_cycle = service.cycle_lead_time(
        workspace_id=principal.workspace_id, project_id=project_id
    )
    return CycleLeadTimeResponse(
        project_id=project_id,
        tasks=tasks,
        average_lead_time_days=avg_lead,
        average_cycle_time_days=avg_cycle,
    )


@router.get("/portfolio/velocity", response_model=PortfolioVelocityResponse)
def portfolio_velocity(
    service: ServiceDep,
    principal: ReaderDep,
    project_ids: Annotated[list[uuid.UUID], Query()],
    last: Annotated[int, Query(ge=1, le=26)] = 6,
) -> PortfolioVelocityResponse:
    summary = service.portfolio_velocity(
        workspace_id=principal.workspace_id, project_ids=project_ids, last=last
    )
    return PortfolioVelocityResponse(**summary.model_dump())


__all__ = ["get_sprint_service", "router"]
