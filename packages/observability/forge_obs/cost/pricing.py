"""Price resolution + exact cost computation (F38 §4).

Resolution rule (spec AC4): newest ``effective_from <= occurred_at``, preferring
a workspace-override row over the global (``workspace_id IS NULL``) default.

Conformance note: the slice doc sketches an async ``PriceBook``; the foundation
uses synchronous SQLAlchemy sessions throughout ``apps/api`` / ``forge_db``, so
the Protocol here is sync (same deviation every DB-backed slice made).
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol, runtime_checkable
from uuid import UUID

from forge_obs.cost.models import ModelPrice, ModelUsage

__all__ = ["DbPriceBook", "InMemoryPriceBook", "PriceBook", "compute_cost"]

_ONE_K = Decimal(1000)
_CENTI_MICRO = Decimal("0.00000001")  # numeric(14,8) scale


def compute_cost(usage: ModelUsage, price: ModelPrice | None) -> Decimal:
    """``(prompt/1k)*prompt_usd_per_1k + (completion/1k)*completion_usd_per_1k``.

    ``price=None`` -> ``Decimal(0)`` (the caller marks ``priced=False`` and
    increments ``forge_unpriced_model_total`` — a gap is visible, never silent).
    """
    if price is None:
        return Decimal(0)
    cost = (Decimal(usage.prompt_tokens) / _ONE_K) * price.prompt_usd_per_1k + (
        Decimal(usage.completion_tokens) / _ONE_K
    ) * price.completion_usd_per_1k
    return cost.quantize(_CENTI_MICRO, rounding=ROUND_HALF_UP)


@runtime_checkable
class PriceBook(Protocol):
    """Resolves the price row in force for a call (spec AC4)."""

    def resolve(
        self,
        *,
        workspace_id: UUID,
        provider: str,
        model: str,
        kind: str,
        at: datetime,
    ) -> ModelPrice | None: ...


class InMemoryPriceBook:
    """Deterministic in-memory price book (tests + no-DB contexts)."""

    def __init__(self, prices: list[ModelPrice] | None = None) -> None:
        self._prices: list[ModelPrice] = list(prices or [])

    def add(self, price: ModelPrice) -> None:
        self._prices.append(price)

    def resolve(
        self,
        *,
        workspace_id: UUID,
        provider: str,
        model: str,
        kind: str,
        at: datetime,
    ) -> ModelPrice | None:
        candidates = [
            p
            for p in self._prices
            if p.provider == provider
            and p.model == model
            and p.kind == kind
            and p.effective_from <= at
            and p.workspace_id in (None, workspace_id)
        ]
        if not candidates:
            return None
        # Workspace override beats global; then newest effective_from wins.
        candidates.sort(key=lambda p: (p.workspace_id is not None, p.effective_from))
        return candidates[-1]


class DbPriceBook:
    """Price book over the durable ``model_price`` table (forge_db)."""

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    def resolve(
        self,
        *,
        workspace_id: UUID,
        provider: str,
        model: str,
        kind: str,
        at: datetime,
    ) -> ModelPrice | None:
        from sqlalchemy import or_, select

        from forge_db.models.cost import ModelPrice as ModelPriceRow

        with self._session_factory() as session:
            stmt = (
                select(ModelPriceRow)
                .where(
                    ModelPriceRow.provider == provider,
                    ModelPriceRow.model == model,
                    ModelPriceRow.kind == kind,
                    ModelPriceRow.effective_from <= at,
                    or_(
                        ModelPriceRow.workspace_id == workspace_id,
                        ModelPriceRow.workspace_id.is_(None),
                    ),
                )
                # Workspace override beats global; then newest effective_from.
                .order_by(
                    ModelPriceRow.workspace_id.is_(None).asc(),
                    ModelPriceRow.effective_from.desc(),
                )
                .limit(1)
            )
            row = session.scalars(stmt).first()
            if row is None:
                return None
            return ModelPrice(
                id=row.id,
                workspace_id=row.workspace_id,
                provider=row.provider,
                model=row.model,
                kind=row.kind.value if hasattr(row.kind, "value") else row.kind,
                prompt_usd_per_1k=row.prompt_usd_per_1k,
                completion_usd_per_1k=row.completion_usd_per_1k,
                currency=row.currency,
                effective_from=row.effective_from,
            )
