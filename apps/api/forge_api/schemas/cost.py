"""Request/response schemas for the F38 Cost API (re-exporting forge_obs DTOs)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field

from forge_obs.cost.models import (
    CostBucket,
    CostRecord,
    CostSummary,
    CostTimeseries,
    ModelPrice,
    ModelUsage,
)

__all__ = [
    "CostBucket",
    "CostRecord",
    "CostSummary",
    "CostTimeseries",
    "ModelPrice",
    "ModelPriceListResponse",
    "ModelUsage",
    "PriceCreateRequest",
    "RepriceRequest",
    "RepriceResponse",
]


class PriceCreateRequest(BaseModel):
    """Admin: add a workspace price override (global rows are seeded/ops-owned)."""

    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=255)
    kind: str = "completion"  # completion | embedding | rerank
    prompt_usd_per_1k: Decimal = Decimal(0)
    completion_usd_per_1k: Decimal = Decimal(0)
    currency: str = "USD"
    effective_from: datetime | None = None  # None -> now


class ModelPriceListResponse(BaseModel):
    items: list[ModelPrice] = Field(default_factory=list)


class RepriceRequest(BaseModel):
    """Admin: re-price historical cost_event rows from the current price book."""

    since: datetime
    provider: str | None = None
    model: str | None = None


class RepriceResponse(BaseModel):
    updated: int
    workspace_id: UUID
