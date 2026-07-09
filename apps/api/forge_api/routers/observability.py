"""Observability + audit routes (Task 1.14 — observability).

Serves the immutable, hash-chained audit log and step-level run traces for the
trace viewer. Both endpoints are authenticated and return secret-redacted data.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import PlainTextResponse

from forge_api.auth.rbac import Permission
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
from forge_contracts import RealtimeEvent
from forge_obs.metrics import RecordingMetrics, get_metrics, render_prometheus

router = APIRouter(
    prefix="/observability",
    tags=["observability"],
    dependencies=[Depends(get_current_principal)],
)

ServiceDep = Annotated[ObservabilityService, Depends(get_observability_service)]

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
