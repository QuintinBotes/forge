"""Cost DTOs (F38 §4 — frozen shapes shared by the meter, ledger, and API)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "COST_KINDS",
    "CostBucket",
    "CostRecord",
    "CostSummary",
    "CostTimeseries",
    "ModelPrice",
    "ModelUsage",
]

#: Priced call classes (spec §3.1 ``cost_event.kind``).
COST_KINDS: frozenset[str] = frozenset({"completion", "embedding", "rerank"})


class ModelUsage(BaseModel):
    """One completed model/embedding/rerank call, as reported by the client seam."""

    workspace_id: UUID
    request_id: str  # idempotency key (provider response id / generated)
    provider: str
    model: str
    kind: str = "completion"  # completion | embedding | rerank
    prompt_tokens: int = 0
    completion_tokens: int = 0
    occurred_at: datetime
    project_id: UUID | None = None
    task_id: UUID | None = None
    workflow_run_id: UUID | None = None
    agent_run_id: UUID | None = None
    step_id: UUID | None = None
    phase: str | None = None
    #: Adaptive Orchestration (ao-observability): the seniority tier
    #: (junior|medior|senior) and strategy (single|swarm) the ExecutionPlan
    #: resolved for the role that made this call. ``None`` for calls made
    #: outside an Adaptive Orchestration plan.
    tier: str | None = None
    strategy: str | None = None


class ModelPrice(BaseModel):
    """A price-book row (global default when ``workspace_id`` is None)."""

    id: UUID | None = None
    workspace_id: UUID | None = None
    provider: str
    model: str
    kind: str = "completion"
    prompt_usd_per_1k: Decimal = Decimal(0)
    completion_usd_per_1k: Decimal = Decimal(0)
    currency: str = "USD"
    effective_from: datetime


class CostRecord(BaseModel):
    """Returned by ``UsageMeter.record`` — what F06's step sink stamps onto the step.

    ``cost_event_id`` is ``None`` only on a guarded (non-strict) ledger-write
    failure; ``deduplicated`` marks an idempotent replay (row already existed,
    counters were NOT re-incremented).
    """

    cost_event_id: UUID | None = None
    cost_usd: Decimal = Decimal(0)
    priced: bool = False  # False when no price matched (cost_usd == 0)
    price_id: UUID | None = None
    deduplicated: bool = False


class CostBucket(BaseModel):
    """One breakdown bucket (phase | provider | model | tier | strategy, per ``group_by``)."""

    key: str
    cost_usd: Decimal
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: Number of priced calls folded into this bucket (Adaptive Orchestration
    #: ao-observability: lets a routing-decisions view show call counts, not
    #: just spend, per tier/strategy/model).
    request_count: int = 0


class CostSummary(BaseModel):
    """Aggregate spend for a scope, with a grouped breakdown."""

    model_config = ConfigDict(populate_by_name=True)

    scope: str  # workspace | project | task
    scope_id: UUID
    total_cost_usd: Decimal = Decimal(0)
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    group_by: str = "none"  # phase | provider | model | none
    buckets: list[CostBucket] = Field(default_factory=list)
    from_: datetime | None = Field(default=None, alias="from")
    to: datetime | None = None


class CostTimeseries(BaseModel):
    """Bucketed spend over time, one series per ``group_by`` key."""

    scope: str
    scope_id: UUID
    bucket: str = "day"  # hour | day | week
    group_by: str = "none"  # provider | model | phase | none
    series: dict[str, list[tuple[datetime, Decimal]]] = Field(default_factory=dict)
