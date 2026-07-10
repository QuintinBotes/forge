"""FX rate resolution for multi-currency budget comparison (F40-OBS-ANALYTICS).

Mirrors ``forge_obs.cost.pricing``'s effective-dated resolution rule (newest
``effective_from <= at``), but for currency conversion rather than model
pricing: the ``cost_event`` ledger is always denominated in USD
(``cost_usd``), so a budget set in another currency needs a rate to compare
against it.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel

__all__ = ["DbFxRateBook", "FxRate", "FxRateBook", "InMemoryFxRateBook", "convert"]


class FxRate(BaseModel):
    """One ``fx_rate`` row: 1 unit of ``base_currency`` == ``rate`` units of ``quote_currency``."""

    id: UUID | None = None
    base_currency: str
    quote_currency: str
    rate: Decimal
    effective_from: datetime


@runtime_checkable
class FxRateBook(Protocol):
    """Resolves the conversion rate in force for a currency pair at a time."""

    def resolve(self, *, base: str, quote: str, at: datetime) -> Decimal | None: ...


def convert(
    amount: Decimal, *, base: str, quote: str, at: datetime, book: FxRateBook
) -> Decimal | None:
    """Convert ``amount`` (in ``base``) to ``quote`` using ``book``.

    Same-currency conversion is always exact (``amount`` unchanged, no lookup).
    Returns ``None`` when no rate (direct or inverse) is in force at ``at``.
    """
    if base == quote:
        return amount
    rate = book.resolve(base=base, quote=quote, at=at)
    if rate is not None:
        return amount * rate
    inverse = book.resolve(base=quote, quote=base, at=at)
    if inverse is not None and inverse != 0:
        return amount / inverse
    return None


class InMemoryFxRateBook:
    """Deterministic in-memory FX rate book (tests + no-DB contexts)."""

    def __init__(self, rates: list[FxRate] | None = None) -> None:
        self._rates: list[FxRate] = list(rates or [])

    def add(self, rate: FxRate) -> None:
        self._rates.append(rate)

    def resolve(self, *, base: str, quote: str, at: datetime) -> Decimal | None:
        candidates = [
            r
            for r in self._rates
            if r.base_currency == base and r.quote_currency == quote and r.effective_from <= at
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda r: r.effective_from).rate


class DbFxRateBook:
    """FX rate book over the real ``fx_rate`` table."""

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    def resolve(self, *, base: str, quote: str, at: datetime) -> Decimal | None:
        from sqlalchemy import select

        from forge_db.models.obs_analytics import FxRate as FxRateRow

        with self._session_factory() as session:
            stmt = (
                select(FxRateRow)
                .where(
                    FxRateRow.base_currency == base,
                    FxRateRow.quote_currency == quote,
                    FxRateRow.effective_from <= at,
                )
                .order_by(FxRateRow.effective_from.desc())
                .limit(1)
            )
            row = session.scalars(stmt).first()
            return Decimal(row.rate) if row is not None else None
