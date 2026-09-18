"""Budget evaluation: spend-vs-cap comparison with hard-cap alerts.

A :class:`Budget` config (workspace/project scope, period, amount, currency,
``hard_cap``) is evaluated against the ``cost_event`` ledger's USD spend for the
same scope, converted to the budget's currency via :mod:`forge_obs.analytics.fx`.
``hard_cap=True`` budgets that are exceeded raise the ``alert`` flag (the caller
wires this to whatever hard-stop / notification path a deployment chooses — the
evaluator itself is pure and stateless).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel

from forge_obs.analytics.fx import FxRateBook, convert

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from forge_db.models.obs_analytics import Budget as BudgetRow

__all__ = ["Budget", "BudgetStatus", "SqlBudgetReader", "evaluate_budget"]


class Budget(BaseModel):
    """A recurring spend cap (mirrors the ``budget`` table)."""

    id: UUID | None = None
    workspace_id: UUID
    name: str
    scope: str = "workspace"  # workspace | project
    project_id: UUID | None = None
    period: str = "monthly"  # daily | weekly | monthly
    amount: Decimal
    currency: str = "USD"
    hard_cap: bool = True


class BudgetStatus(BaseModel):
    """The evaluated state of a budget against a period's actual spend."""

    budget_id: UUID | None = None
    spend_usd: Decimal
    spend_in_budget_currency: Decimal | None = None
    amount: Decimal
    currency: str
    pct_used: float | None = None
    over_budget: bool = False
    hard_cap: bool = True
    alert: bool = False
    fx_unavailable: bool = False


def evaluate_budget(
    budget: Budget,
    spend_usd: Decimal,
    *,
    fx: FxRateBook,
    at: datetime,
) -> BudgetStatus:
    """Compare ``spend_usd`` against ``budget``, converting currency via ``fx``.

    When no FX rate is available for a non-USD budget, ``spend_in_budget_currency``
    and ``pct_used`` are left ``None`` and ``fx_unavailable=True`` — the caller
    never silently compares mismatched currencies, and (per the "hard-cap alerts
    can never be silently skipped" principle) a hard-cap budget with unresolved
    FX still raises ``alert`` so an operator is not left blind by a pricing gap.
    """
    converted = convert(spend_usd, base="USD", quote=budget.currency, at=at, book=fx)
    if converted is None:
        return BudgetStatus(
            budget_id=budget.id,
            spend_usd=spend_usd,
            amount=budget.amount,
            currency=budget.currency,
            hard_cap=budget.hard_cap,
            alert=budget.hard_cap,
            fx_unavailable=True,
        )

    pct_used = float(converted / budget.amount) if budget.amount else None
    over_budget = budget.amount > 0 and converted > budget.amount
    return BudgetStatus(
        budget_id=budget.id,
        spend_usd=spend_usd,
        spend_in_budget_currency=converted,
        amount=budget.amount,
        currency=budget.currency,
        pct_used=pct_used,
        over_budget=over_budget,
        hard_cap=budget.hard_cap,
        alert=budget.hard_cap and over_budget,
    )


class SqlBudgetReader:
    """List/get ``budget`` rows for a workspace."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def _dto(self, row: BudgetRow) -> Budget:
        return Budget(
            id=row.id,
            workspace_id=row.workspace_id,
            name=row.name,
            scope=row.scope.value if hasattr(row.scope, "value") else row.scope,
            project_id=row.project_id,
            period=row.period.value if hasattr(row.period, "value") else row.period,
            amount=Decimal(row.amount),
            currency=row.currency,
            hard_cap=row.hard_cap,
        )

    def get(self, *, workspace_id: UUID, budget_id: UUID) -> Budget | None:
        from sqlalchemy import select

        from forge_db.models.obs_analytics import Budget as BudgetRow

        with self._session_factory() as session:
            stmt = select(BudgetRow).where(
                BudgetRow.id == budget_id, BudgetRow.workspace_id == workspace_id
            )
            row = session.scalars(stmt).first()
            return None if row is None else self._dto(row)

    def list(self, *, workspace_id: UUID) -> list[Budget]:
        from sqlalchemy import select

        from forge_db.models.obs_analytics import Budget as BudgetRow

        with self._session_factory() as session:
            stmt = select(BudgetRow).where(BudgetRow.workspace_id == workspace_id)
            return [self._dto(row) for row in session.scalars(stmt)]
