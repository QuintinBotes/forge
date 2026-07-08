"""Cost API router (F38) — the in-product cost surface over the ledger.

Reads (``viewer``+, ``Permission.READ``): per-task cost, scoped summaries, and
timeseries. Mutations (``admin``): price-book writes and reprice, both audited.
Every endpoint is workspace-isolated: a cross-workspace ``scope_id`` returns
**404** (no existence leak, spec AC14). ``/metrics`` (Prometheus exposition) is
infra-internal and NOT part of this authenticated product surface — see
``/observability/metrics``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from forge_api.auth.rbac import Permission
from forge_api.db import get_session_factory
from forge_api.deps import Principal, get_current_principal
from forge_api.routers._rbac import require_permission
from forge_api.schemas.cost import (
    CostSummary,
    CostTimeseries,
    ModelPrice,
    ModelPriceListResponse,
    PriceCreateRequest,
    RepriceRequest,
    RepriceResponse,
)
from forge_api.services.cost_service import (
    CostService,
    ScopeNotFoundError,
    SessionAuditSink,
    SqlPriceStore,
    SqlScopeResolver,
)
from forge_obs.cost.pricing import DbPriceBook
from forge_obs.cost.repository import SqlCostLedger, SqlCostReader

router = APIRouter(tags=["cost"], dependencies=[Depends(get_current_principal)])

ReaderDep = Annotated[Principal, Depends(require_permission(Permission.READ))]
AdminDep = Annotated[Principal, Depends(require_permission(Permission.ADMIN))]


def get_cost_service() -> CostService:
    """Build the DB-backed cost service (overridable in tests via DI)."""
    factory = get_session_factory()
    return CostService(
        reader=SqlCostReader(factory),
        ledger=SqlCostLedger(factory),
        price_book=DbPriceBook(factory),
        prices=SqlPriceStore(factory),
        scopes=SqlScopeResolver(factory),
        audit=SessionAuditSink(factory),
    )


ServiceDep = Annotated[CostService, Depends(get_cost_service)]


def _guard[T](call: Callable[[], T]) -> T:
    """Translate service errors into the API's HTTP contract."""
    try:
        return call()
    except ScopeNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="scope not found"
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc


@router.get(
    "/tasks/{task_id}/cost",
    response_model=CostSummary,
    summary="Per-task spend (grouped by workflow phase).",
)
def task_cost(task_id: uuid.UUID, principal: ReaderDep, service: ServiceDep) -> CostSummary:
    return _guard(lambda: service.task_cost(workspace_id=principal.workspace_id, task_id=task_id))


@router.get(
    "/cost/summary",
    response_model=CostSummary,
    summary="Aggregate spend for a scope with a grouped breakdown.",
)
def cost_summary(
    principal: ReaderDep,
    service: ServiceDep,
    scope: Annotated[str, Query(pattern="^(workspace|project|task)$")] = "workspace",
    scope_id: Annotated[uuid.UUID | None, Query()] = None,
    group_by: Annotated[
        str, Query(pattern="^(phase|provider|model|tier|strategy|none)$")
    ] = "provider",
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
) -> CostSummary:
    resolved_scope_id = scope_id if scope_id is not None else principal.workspace_id
    return _guard(
        lambda: service.summary(
            workspace_id=principal.workspace_id,
            scope=scope,
            scope_id=resolved_scope_id,
            group_by=group_by,
            frm=from_,
            to=to,
        )
    )


@router.get(
    "/cost/timeseries",
    response_model=CostTimeseries,
    summary="Bucketed spend over time, one series per group key.",
)
def cost_timeseries(
    principal: ReaderDep,
    service: ServiceDep,
    scope: Annotated[str, Query(pattern="^(workspace|project|task)$")] = "workspace",
    scope_id: Annotated[uuid.UUID | None, Query()] = None,
    bucket: Annotated[str, Query(pattern="^(hour|day|week)$")] = "day",
    group_by: Annotated[
        str, Query(pattern="^(phase|provider|model|tier|strategy|none)$")
    ] = "provider",
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
) -> CostTimeseries:
    resolved_scope_id = scope_id if scope_id is not None else principal.workspace_id
    return _guard(
        lambda: service.timeseries(
            workspace_id=principal.workspace_id,
            scope=scope,
            scope_id=resolved_scope_id,
            bucket=bucket,
            group_by=group_by,
            frm=from_,
            to=to,
        )
    )


@router.get(
    "/cost/prices",
    response_model=ModelPriceListResponse,
    summary="List the price book visible to this workspace (globals + overrides).",
)
def list_prices(
    principal: ReaderDep,
    service: ServiceDep,
    provider: str | None = Query(default=None),
    model: str | None = Query(default=None),
) -> ModelPriceListResponse:
    items = service.list_prices(workspace_id=principal.workspace_id, provider=provider, model=model)
    return ModelPriceListResponse(items=items)


@router.post(
    "/cost/prices",
    response_model=ModelPrice,
    status_code=status.HTTP_201_CREATED,
    summary="Add a workspace price override (admin; audited as cost.price_set).",
)
def set_price(body: PriceCreateRequest, principal: AdminDep, service: ServiceDep) -> ModelPrice:
    return _guard(
        lambda: service.set_price(
            workspace_id=principal.workspace_id,
            actor_id=principal.user_id,
            data=body,
        )
    )


@router.post(
    "/cost/reprice",
    response_model=RepriceResponse,
    summary="Re-price historical cost events (admin; audited as cost.repriced).",
)
def reprice(body: RepriceRequest, principal: AdminDep, service: ServiceDep) -> RepriceResponse:
    updated = _guard(
        lambda: service.reprice(
            workspace_id=principal.workspace_id,
            actor_id=principal.user_id,
            since=body.since,
            provider=body.provider,
            model=body.model,
        )
    )
    return RepriceResponse(updated=updated, workspace_id=principal.workspace_id)
