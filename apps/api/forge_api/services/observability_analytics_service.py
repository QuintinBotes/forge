"""F40-OBS-ANALYTICS service: workspace-isolated reads over the deep-analytics
backends in ``forge_obs.analytics``.

Mirrors ``CostService``'s isolation discipline (reusing its ``ScopeResolver``
Protocol/``SqlScopeResolver`` directly rather than re-deriving it): every read
is scoped by the authenticated principal's ``workspace_id``; a
``project_id``/``budget_id`` belonging to another workspace (or nothing)
surfaces as :class:`ScopeNotFoundError` -> HTTP 404 (no existence leak).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from forge_api.services.cost_service import ScopeResolver
from forge_obs.analytics.budgets import Budget, BudgetStatus, SqlBudgetReader, evaluate_budget
from forge_obs.analytics.coverage import (
    CoverageSnapshotDTO,
    CoverageTrendPoint,
    SqlCoverageRepository,
)
from forge_obs.analytics.dora import DoraMetrics, SqlDoraReader
from forge_obs.analytics.fx import FxRateBook
from forge_obs.analytics.incidents import IncidentReliabilityMetrics, SqlIncidentReliabilityReader
from forge_obs.cost.repository import CostReader

__all__ = ["ObservabilityAnalyticsService", "ScopeNotFoundError"]

#: Approximate lookback window per budget period (a calendar-exact rolling
#: period is a product decision left to a later slice; documented here rather
#: than silently assumed).
_PERIOD_DAYS: dict[str, int] = {"daily": 1, "weekly": 7, "monthly": 30}


class ScopeNotFoundError(LookupError):
    """Unknown scope id OR a cross-workspace scope id (both -> 404)."""


class ObservabilityAnalyticsService:
    """Workspace-isolated reads over incident reliability, DORA, budgets, coverage."""

    def __init__(
        self,
        *,
        incidents: SqlIncidentReliabilityReader,
        dora: SqlDoraReader,
        budgets: SqlBudgetReader,
        coverage: SqlCoverageRepository,
        cost_reader: CostReader,
        fx: FxRateBook,
        scopes: ScopeResolver,
    ) -> None:
        self._incidents = incidents
        self._dora = dora
        self._budgets = budgets
        self._coverage = coverage
        self._cost_reader = cost_reader
        self._fx = fx
        self._scopes = scopes

    def _check_project(self, workspace_id: UUID, project_id: UUID | None) -> None:
        if project_id is None:
            return
        owner = self._scopes.workspace_of("project", project_id)
        if owner is None or owner != workspace_id:
            raise ScopeNotFoundError(project_id)

    def incident_reliability(
        self,
        *,
        workspace_id: UUID,
        project_id: UUID | None,
        frm: datetime | None,
        to: datetime | None,
    ) -> IncidentReliabilityMetrics:
        self._check_project(workspace_id, project_id)
        return self._incidents.reliability(
            workspace_id=workspace_id, project_id=project_id, frm=frm, to=to
        )

    def dora_metrics(
        self,
        *,
        workspace_id: UUID,
        project_id: UUID | None,
        frm: datetime | None,
        to: datetime | None,
    ) -> DoraMetrics:
        self._check_project(workspace_id, project_id)
        return self._dora.dora_metrics(
            workspace_id=workspace_id, project_id=project_id, frm=frm, to=to
        )

    def budget_status(self, *, workspace_id: UUID, budget_id: UUID) -> BudgetStatus:
        budget = self._budgets.get(workspace_id=workspace_id, budget_id=budget_id)
        if budget is None:
            raise ScopeNotFoundError(budget_id)
        return self._evaluate(budget)

    def list_budget_statuses(self, *, workspace_id: UUID) -> list[BudgetStatus]:
        return [self._evaluate(b) for b in self._budgets.list(workspace_id=workspace_id)]

    def _evaluate(self, budget: Budget) -> BudgetStatus:
        now = datetime.now(UTC)
        window_days = _PERIOD_DAYS.get(budget.period, 30)
        since = now - timedelta(days=window_days)
        scope, scope_id = (
            ("project", budget.project_id)
            if budget.project_id
            else ("workspace", budget.workspace_id)
        )
        spend = self._cost_reader.summary(
            workspace_id=budget.workspace_id,
            scope=scope,
            scope_id=scope_id,
            group_by="none",
            frm=since,
            to=now,
        )
        return evaluate_budget(budget, Decimal(spend.total_cost_usd), fx=self._fx, at=now)

    def coverage_trend(
        self,
        *,
        workspace_id: UUID,
        project_id: UUID,
        repo_id: str | None,
        frm: date | None,
        to: date | None,
    ) -> list[CoverageSnapshotDTO]:
        self._check_project(workspace_id, project_id)
        return self._coverage.trend(project_id=project_id, repo_id=repo_id, frm=frm, to=to)

    def coverage_rollup(
        self, *, workspace_id: UUID, frm: date | None, to: date | None
    ) -> list[CoverageTrendPoint]:
        return self._coverage.org_rollup(workspace_id=workspace_id, frm=frm, to=to)
