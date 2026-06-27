"""Observability + audit routes (Task 1.14 — observability).

Serves the immutable, hash-chained audit log and step-level run traces for the
trace viewer. Both endpoints are authenticated and return secret-redacted data.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from forge_api.deps import CurrentPrincipal, get_current_principal
from forge_api.observability.audit import AuditCategory, AuditEntry
from forge_api.observability.service import (
    ObservabilityService,
    RunNotFoundError,
    get_observability_service,
)
from forge_api.observability.trace import RunTrace

router = APIRouter(
    prefix="/observability",
    tags=["observability"],
    dependencies=[Depends(get_current_principal)],
)

ServiceDep = Annotated[ObservabilityService, Depends(get_observability_service)]


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
    return service.query_audit(
        category=category,
        actor=actor,
        run_id=run_id,
        connection_id=connection_id,
        limit=limit,
    )


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
    try:
        return service.get_run_trace(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No run trace recorded for run {run_id}",
        ) from exc
