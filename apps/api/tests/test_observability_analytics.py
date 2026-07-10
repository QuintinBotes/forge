"""F40-OBS-ANALYTICS API surface: incident reliability, DORA, budgets, coverage.

Handler tests run hermetically over in-memory/fake seams injected through
``get_observability_analytics_service`` (the same DI seam the production Sql
wiring uses — the Sql implementations are exercised against real Postgres in
``packages/observability``), mirroring the F38 Cost API test convention.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from forge_api.deps import Principal, get_current_principal
from forge_api.main import create_app
from forge_api.routers.incidents import IncidentServiceRegistry, get_incident_registry
from forge_api.routers.observability import get_observability_analytics_service
from forge_api.services.observability_analytics_service import ObservabilityAnalyticsService
from forge_contracts import UserRole
from forge_contracts.enums import IncidentSeverity
from forge_obs.analytics.budgets import Budget, SqlBudgetReader
from forge_obs.analytics.coverage import CoverageSnapshotDTO, CoverageTrendPoint
from forge_obs.analytics.dora import DoraMetrics
from forge_obs.analytics.incidents import IncidentReliabilityMetrics

WS_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
OTHER_WS_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a2")
PROJECT_ID = uuid.uuid4()
NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# IncidentService.reliability_metrics (in-memory seam, F17 timestamps)         #
# --------------------------------------------------------------------------- #


@pytest.fixture
def registry() -> IncidentServiceRegistry:
    return IncidentServiceRegistry()


def _build_incident_client(registry: IncidentServiceRegistry) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        user_id=uuid.uuid4(),
        workspace_id=WS_ID,
        role=UserRole.MEMBER,
        email="obs-analytics@forge.local",
        auth_method="test",
        scopes=["*"],
    )
    app.dependency_overrides[get_incident_registry] = lambda: registry
    return TestClient(app)


def test_incident_service_reliability_metrics_aggregates_in_memory_records(
    registry: IncidentServiceRegistry,
) -> None:
    service = registry.for_workspace(WS_ID)
    incident = service.declare(
        project_id=PROJECT_ID, title="db down", severity=IncidentSeverity.HIGH
    )
    incident.acknowledged_at = incident.detected_at + timedelta(minutes=5)
    incident.resolved_at = incident.detected_at + timedelta(hours=1)
    service.propose_remediation(
        incident.id,
        steps=[],
    )
    service.latest_plan(incident.id).status = "approved"

    metrics = service.reliability_metrics(project_id=PROJECT_ID)
    assert metrics.sample_size == 1
    assert metrics.mtta_seconds == pytest.approx(300)
    assert metrics.mttr_seconds == pytest.approx(3600)
    assert metrics.remediation_total == 1
    assert metrics.remediation_accepted == 1
    assert metrics.remediation_accept_rate == pytest.approx(1.0)


def test_incident_reliability_endpoint_returns_zero_metrics_when_no_incidents(
    registry: IncidentServiceRegistry,
) -> None:
    with _build_incident_client(registry) as client:
        resp = client.get("/incidents/reliability")
    assert resp.status_code == 200
    body = resp.json()
    assert body["sample_size"] == 0
    assert body["mtta_seconds"] is None
    assert body["remediation_accept_rate"] is None


def test_incident_reliability_endpoint_reflects_declared_incident(
    registry: IncidentServiceRegistry,
) -> None:
    service = registry.for_workspace(WS_ID)
    service.declare(project_id=PROJECT_ID, title="outage", severity=IncidentSeverity.CRITICAL)

    with _build_incident_client(registry) as client:
        resp = client.get("/incidents/reliability", params={"project_id": str(PROJECT_ID)})
    assert resp.status_code == 200
    assert resp.json()["sample_size"] == 1


# --------------------------------------------------------------------------- #
# ObservabilityAnalyticsService router: DORA, budgets, coverage                #
# --------------------------------------------------------------------------- #


class _FakeIncidentReader:
    def reliability(self, **_kwargs) -> IncidentReliabilityMetrics:
        return IncidentReliabilityMetrics(sample_size=0)


class _FakeDoraReader:
    def dora_metrics(self, **_kwargs) -> DoraMetrics:
        return DoraMetrics(
            deployment_count=10,
            successful_count=8,
            deploy_frequency_per_day=2.0,
            lead_time_seconds=600.0,
            change_failure_rate=0.2,
            mttr_seconds=1800.0,
        )


class _FakeCoverageRepo:
    def trend(self, **_kwargs) -> list[CoverageSnapshotDTO]:
        return [
            CoverageSnapshotDTO(
                project_id=PROJECT_ID,
                repo_id="org/forge",
                snapshot_date=NOW.date(),
                lines_covered=80,
                lines_total=100,
                coverage_pct=80.0,
            )
        ]

    def org_rollup(self, **_kwargs) -> list[CoverageTrendPoint]:
        return [
            CoverageTrendPoint(
                snapshot_date=NOW.date(), lines_covered=80, lines_total=100, coverage_pct=80.0
            )
        ]


class _FakeCostSummary:
    def __init__(self, total: str) -> None:
        self.total_cost_usd = Decimal(total)


class _FakeCostReader:
    def summary(self, **_kwargs) -> _FakeCostSummary:
        return _FakeCostSummary("1200")


class _FakeFx:
    def resolve(self, **_kwargs):
        return None  # same-currency budgets never need a lookup in these tests


class _FakeScopeResolver:
    def workspace_of(self, scope: str, scope_id: uuid.UUID) -> uuid.UUID | None:
        del scope
        return WS_ID if scope_id == PROJECT_ID else None


@pytest.fixture
def analytics_budgets() -> SqlBudgetReader:
    """An in-memory stand-in exposing the same ``get``/``list`` surface."""

    class _InMemoryBudgets:
        def __init__(self) -> None:
            self.rows: dict[uuid.UUID, Budget] = {}

        def add(self, budget: Budget) -> Budget:
            self.rows[budget.id] = budget
            return budget

        def get(self, *, workspace_id: uuid.UUID, budget_id: uuid.UUID) -> Budget | None:
            row = self.rows.get(budget_id)
            return row if row is not None and row.workspace_id == workspace_id else None

        def list(self, *, workspace_id: uuid.UUID) -> list[Budget]:
            return [b for b in self.rows.values() if b.workspace_id == workspace_id]

    return _InMemoryBudgets()  # type: ignore[return-value]


@pytest.fixture
def analytics_service(analytics_budgets) -> ObservabilityAnalyticsService:
    return ObservabilityAnalyticsService(
        incidents=_FakeIncidentReader(),  # type: ignore[arg-type]
        dora=_FakeDoraReader(),  # type: ignore[arg-type]
        budgets=analytics_budgets,
        coverage=_FakeCoverageRepo(),  # type: ignore[arg-type]
        cost_reader=_FakeCostReader(),  # type: ignore[arg-type]
        fx=_FakeFx(),  # type: ignore[arg-type]
        scopes=_FakeScopeResolver(),  # type: ignore[arg-type]
    )


@pytest.fixture
def analytics_client(
    analytics_service: ObservabilityAnalyticsService,
) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def _build(workspace_id: uuid.UUID = WS_ID) -> TestClient:
        app = create_app()
        app.dependency_overrides[get_observability_analytics_service] = lambda: analytics_service
        app.dependency_overrides[get_current_principal] = lambda: Principal(
            user_id=uuid.uuid4(),
            workspace_id=workspace_id,
            role=UserRole.MEMBER,
            email="obs-analytics@forge.local",
            auth_method="test",
            scopes=["*"],
        )
        tc = TestClient(app)
        clients.append(tc)
        return tc

    yield _build
    for tc in clients:
        tc.close()


def test_dora_endpoint_returns_the_four_dora_keys(
    analytics_client: Callable[..., TestClient],
) -> None:
    resp = analytics_client().get("/observability/analytics/dora")
    assert resp.status_code == 200
    body = resp.json()
    assert body["deployment_count"] == 10
    assert body["change_failure_rate"] == pytest.approx(0.2)


def test_coverage_trend_and_rollup_endpoints(analytics_client: Callable[..., TestClient]) -> None:
    client = analytics_client()
    trend = client.get(
        "/observability/analytics/coverage/trend", params={"project_id": str(PROJECT_ID)}
    )
    assert trend.status_code == 200
    assert trend.json()[0]["coverage_pct"] == 80.0

    rollup = client.get("/observability/analytics/coverage/rollup")
    assert rollup.status_code == 200
    assert rollup.json()[0]["coverage_pct"] == 80.0


def test_dora_endpoint_rejects_a_foreign_project_scope(
    analytics_client: Callable[..., TestClient],
) -> None:
    resp = analytics_client().get(
        "/observability/analytics/dora", params={"project_id": str(uuid.uuid4())}
    )
    assert resp.status_code == 404


def test_budget_status_endpoint_evaluates_hard_cap_alert(
    analytics_client: Callable[..., TestClient], analytics_budgets
) -> None:
    budget = analytics_budgets.add(
        Budget(
            id=uuid.uuid4(),
            workspace_id=WS_ID,
            name="Monthly cap",
            amount=Decimal("1000"),
            currency="USD",
            hard_cap=True,
        )
    )
    resp = analytics_client().get(f"/observability/analytics/budgets/{budget.id}/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["over_budget"] is True
    assert body["alert"] is True


def test_budget_status_endpoint_404s_for_unknown_budget(
    analytics_client: Callable[..., TestClient],
) -> None:
    resp = analytics_client().get(f"/observability/analytics/budgets/{uuid.uuid4()}/status")
    assert resp.status_code == 404


def test_budget_status_endpoint_404s_across_workspaces(
    analytics_client: Callable[..., TestClient], analytics_budgets
) -> None:
    budget = analytics_budgets.add(
        Budget(id=uuid.uuid4(), workspace_id=OTHER_WS_ID, name="Other", amount=Decimal("500"))
    )
    resp = analytics_client().get(f"/observability/analytics/budgets/{budget.id}/status")
    assert resp.status_code == 404
