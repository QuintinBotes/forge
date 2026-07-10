"""Observability + audit routes (Task 1.14 — observability).

Serves the immutable, hash-chained audit log and step-level run traces for the
trace viewer. Both endpoints are authenticated and return secret-redacted data.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import PlainTextResponse

from forge_api.auth.rbac import Permission
from forge_api.db import get_session_factory
from forge_api.deps import CurrentPrincipal, Principal, get_current_principal
from forge_api.observability.audit import AuditCategory, AuditEntry
from forge_api.observability.service import (
    ObservabilityService,
    RunNotFoundError,
    get_observability_service,
    run_event_type_for_status,
)
from forge_api.observability.trace import RunTrace
from forge_api.realtime.broadcaster import Broadcaster, emit_event, get_broadcaster
from forge_api.routers._rbac import require_permission
from forge_api.schemas.observability import RecordRunRequest
from forge_api.services.cost_service import SqlScopeResolver
from forge_api.services.observability_analytics_service import (
    ObservabilityAnalyticsService,
    ScopeNotFoundError,
)
from forge_contracts import RealtimeEvent
from forge_obs.analytics.budgets import BudgetStatus, SqlBudgetReader
from forge_obs.analytics.coverage import (
    CoverageSnapshotDTO,
    CoverageTrendPoint,
    SqlCoverageRepository,
)
from forge_obs.analytics.dora import DoraMetrics, SqlDoraReader
from forge_obs.analytics.fx import DbFxRateBook
from forge_obs.analytics.incidents import IncidentReliabilityMetrics, SqlIncidentReliabilityReader
from forge_obs.cost.repository import SqlCostReader
from forge_obs.metrics import RecordingMetrics, get_metrics, render_prometheus

router = APIRouter(
    prefix="/observability",
    tags=["observability"],
    dependencies=[Depends(get_current_principal)],
)

ServiceDep = Annotated[ObservabilityService, Depends(get_observability_service)]
ReaderDep = Annotated[Principal, Depends(require_permission(Permission.READ))]


def get_observability_analytics_service() -> ObservabilityAnalyticsService:
    """Build the DB-backed F40 deep-analytics service (overridable in tests via DI)."""
    factory = get_session_factory()
    return ObservabilityAnalyticsService(
        incidents=SqlIncidentReliabilityReader(factory),
        dora=SqlDoraReader(factory),
        budgets=SqlBudgetReader(factory),
        coverage=SqlCoverageRepository(factory),
        cost_reader=SqlCostReader(factory),
        fx=DbFxRateBook(factory),
        scopes=SqlScopeResolver(factory),
    )


AnalyticsDep = Annotated[
    ObservabilityAnalyticsService, Depends(get_observability_analytics_service)
]


def _guard[T](call: Callable[[], T]) -> T:
    """Translate a cross-workspace/unknown scope into the API's 404 contract."""
    try:
        return call()
    except ScopeNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="scope not found"
        ) from exc


# Recording a run trace is the run producer's write (agent/workflow runtime),
# so it is RUN_AGENT-gated (the ``agent-runner`` role, which lacks WRITE, can
# still post its own run's trace) rather than the generic WRITE permission.
RunnerDep = Annotated[Principal, Depends(require_permission(Permission.RUN_AGENT))]

# RT-7: fan run-trace updates out to the workspace's live ``/ws`` sockets.
BroadcasterDep = Annotated[Broadcaster, Depends(get_broadcaster)]


@router.get(
    "/audit",
    response_model=list[AuditEntry],
    summary="Query the immutable, redacted audit log.",
)
def list_audit(
    principal: CurrentPrincipal,
    service: ServiceDep,
    category: AuditCategory | None = None,
    actor: str | None = None,
    run_id: uuid.UUID | None = None,
    connection_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[AuditEntry]:
    # Per-workspace isolation: a caller may only read their own tenant's audit
    # entries. The principal's workspace scopes the query so the cross-tenant
    # audit log never leaks (every entry carries its actor's ``workspace_id``).
    return service.query_audit(
        category=category,
        actor=actor,
        run_id=run_id,
        connection_id=connection_id,
        workspace_id=principal.workspace_id,
        limit=limit,
    )


@router.get(
    "/metrics",
    summary="Prometheus text exposition of the in-process F38 metric registry.",
    response_class=PlainTextResponse,
)
def prometheus_metrics(principal: CurrentPrincipal) -> PlainTextResponse:
    """Internal scrape surface (F38).

    Deviation from the slice doc, noted: instead of an unauthenticated
    ``/metrics`` bound to an internal network interface (an infra concern the
    compose profile owns), the in-app exposition mounts behind the normal
    authenticated router so it can never leak on a public deployment. With
    observability disabled (``OBS_ENABLED=false``) the registry is a no-op and
    the body is empty — no export is attempted (spec AC18).
    """
    del principal  # authentication is the gate; metrics carry no tenant data
    metrics = get_metrics()
    body = render_prometheus(metrics) if isinstance(metrics, RecordingMetrics) else ""
    return PlainTextResponse(content=body, media_type="text/plain; version=0.0.4")


@router.get(
    "/runs/{run_id}/trace",
    response_model=RunTrace,
    summary="Assemble a step-level run trace.",
)
def get_run_trace(
    run_id: uuid.UUID,
    principal: CurrentPrincipal,
    service: ServiceDep,
) -> RunTrace:
    # Scope the lookup to the caller's workspace: a foreign (or unknown) run id
    # surfaces as 404 so one tenant cannot fetch another tenant's run trace.
    try:
        return service.get_run_trace(run_id, workspace_id=principal.workspace_id)
    except RunNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No run trace recorded for run {run_id}",
        ) from exc


@router.post(
    "/runs/{run_id}/trace",
    response_model=RunTrace,
    status_code=status.HTTP_201_CREATED,
    summary="Record/update a run's step trace and fan out its run.* event.",
)
async def record_run_trace(
    run_id: uuid.UUID,
    principal: RunnerDep,
    service: ServiceDep,
    broadcaster: BroadcasterDep,
    body: RecordRunRequest,
) -> RunTrace:
    """The run-trace producer: agent/workflow runtimes post step updates here.

    Assembles (or re-assembles) the run's trace, scoped to the caller's
    workspace, then fans a ``run.*`` realtime event out to the workspace's live
    ``/ws`` sockets — ``run.started``/``run.updated`` while in flight,
    ``run.completed``/``run.failed`` once ``status`` reaches a terminal state.
    """
    trace = service.record_run(
        run_id,
        steps=body.steps,
        status=body.status,
        confidence=body.confidence,
        workspace_id=principal.workspace_id,
    )
    await emit_event(
        broadcaster,
        RealtimeEvent(
            type=run_event_type_for_status(body.status),
            workspace_id=principal.workspace_id,
            run_id=run_id,
            payload={"status": body.status.value if body.status else None},
        ),
    )
    return trace


# --------------------------------------------------------------------------- #
# F40-OBS-ANALYTICS: deep-analytics backends                                  #
# --------------------------------------------------------------------------- #


@router.get(
    "/analytics/incidents/reliability",
    response_model=IncidentReliabilityMetrics,
    summary="MTTA/MTTR/remediation-accept-rate over the persisted incident tables.",
)
def incident_reliability_sql(
    principal: ReaderDep,
    service: AnalyticsDep,
    project_id: Annotated[uuid.UUID | None, Query()] = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
) -> IncidentReliabilityMetrics:
    return _guard(
        lambda: service.incident_reliability(
            workspace_id=principal.workspace_id, project_id=project_id, frm=from_, to=to
        )
    )


@router.get(
    "/analytics/dora",
    response_model=DoraMetrics,
    summary="DORA metrics: deploy frequency, lead time, change-failure rate, MTTR.",
)
def dora_metrics(
    principal: ReaderDep,
    service: AnalyticsDep,
    project_id: Annotated[uuid.UUID | None, Query()] = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
) -> DoraMetrics:
    return _guard(
        lambda: service.dora_metrics(
            workspace_id=principal.workspace_id, project_id=project_id, frm=from_, to=to
        )
    )


@router.get(
    "/analytics/budgets/{budget_id}/status",
    response_model=BudgetStatus,
    summary="Evaluate a budget's current-period spend against its cap.",
)
def budget_status(
    budget_id: uuid.UUID, principal: ReaderDep, service: AnalyticsDep
) -> BudgetStatus:
    return _guard(
        lambda: service.budget_status(workspace_id=principal.workspace_id, budget_id=budget_id)
    )


@router.get(
    "/analytics/budgets",
    response_model=list[BudgetStatus],
    summary="Evaluate every budget defined for this workspace.",
)
def list_budget_statuses(principal: ReaderDep, service: AnalyticsDep) -> list[BudgetStatus]:
    return service.list_budget_statuses(workspace_id=principal.workspace_id)


@router.get(
    "/analytics/coverage/trend",
    response_model=list[CoverageSnapshotDTO],
    summary="Per-day test-coverage trend for a project (optionally scoped to a repo).",
)
def coverage_trend(
    project_id: uuid.UUID,
    principal: ReaderDep,
    service: AnalyticsDep,
    repo_id: str | None = None,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: Annotated[date | None, Query()] = None,
) -> list[CoverageSnapshotDTO]:
    return _guard(
        lambda: service.coverage_trend(
            workspace_id=principal.workspace_id,
            project_id=project_id,
            repo_id=repo_id,
            frm=from_,
            to=to,
        )
    )


@router.get(
    "/analytics/coverage/rollup",
    response_model=list[CoverageTrendPoint],
    summary="Workspace-wide per-day coverage rollup, weighted by lines across repos.",
)
def coverage_rollup(
    principal: ReaderDep,
    service: AnalyticsDep,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: Annotated[date | None, Query()] = None,
) -> list[CoverageTrendPoint]:
    return service.coverage_rollup(workspace_id=principal.workspace_id, frm=from_, to=to)
