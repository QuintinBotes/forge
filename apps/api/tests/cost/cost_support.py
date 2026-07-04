"""Shared identities + fixtures for the F38 Cost API tests.

Deliberately NOT a ``conftest.py``: the sibling marketplace test dir imports
its own conftest by module name (``from conftest import ...``), and a second
package-less ``conftest.py`` under ``apps/api/tests`` makes
``sys.modules['conftest']`` order-dependent across whole-repo runs. This
uniquely-named module holds constants + pytest fixtures; the test module
imports them explicitly (imported fixtures are discovered by pytest).

Handler tests run hermetically over the in-memory seams (ledger, price store,
scope resolver, capturing audit sink) injected through ``get_cost_service`` —
the same DI seam the production Sql wiring uses (the Sql implementations are
exercised against real Postgres in ``packages/observability`` / ``packages/db``).
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
from forge_api.routers.cost import get_cost_service
from forge_api.services.cost_service import (
    CostService,
    InMemoryPriceStore,
    InMemoryScopeResolver,
)
from forge_contracts import UserRole
from forge_contracts.audit import AuditEvent
from forge_obs.cost.models import ModelPrice, ModelUsage
from forge_obs.cost.pricing import InMemoryPriceBook
from forge_obs.cost.repository import InMemoryCostLedger

WS_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
OTHER_WS_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a2")
PROJECT_ID = uuid.uuid4()
TASK_ID = uuid.uuid4()
FOREIGN_TASK_ID = uuid.uuid4()
NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


class CapturingAuditSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def emit(self, event: AuditEvent) -> None:
        self.events.append(event)


@pytest.fixture
def ledger() -> InMemoryCostLedger:
    ledger = InMemoryCostLedger()
    rows = [
        ("r1", "anthropic", "claude-sonnet-4-5", "spec_drafting", NOW, "0.04"),
        ("r2", "anthropic", "claude-sonnet-4-5", "executing", NOW + timedelta(hours=1), "0.28"),
        ("r3", "openai", "text-embedding-3-small", "executing",
         NOW + timedelta(days=1), "0.06"),
        ("r4", "anthropic", "claude-sonnet-4-5", "verifying",
         NOW + timedelta(days=1, hours=2), "0.05"),
    ]
    for request_id, provider, model, phase, occurred, cost in rows:
        ledger.upsert_event(
            ModelUsage(
                workspace_id=WS_ID,
                request_id=request_id,
                provider=provider,
                model=model,
                kind="completion",
                prompt_tokens=1000,
                completion_tokens=100,
                occurred_at=occurred,
                project_id=PROJECT_ID,
                task_id=TASK_ID,
                phase=phase,
            ),
            cost=Decimal(cost),
            price_id=None,
        )
    return ledger


@pytest.fixture
def price_book() -> InMemoryPriceBook:
    return InMemoryPriceBook(
        [
            ModelPrice(
                id=uuid.uuid4(),
                provider="anthropic",
                model="claude-sonnet-4-5",
                kind="completion",
                prompt_usd_per_1k=Decimal("0.003"),
                completion_usd_per_1k=Decimal("0.015"),
                effective_from=NOW - timedelta(days=30),
            )
        ]
    )


@pytest.fixture
def price_store() -> InMemoryPriceStore:
    store = InMemoryPriceStore()
    store.prices.append(
        ModelPrice(
            id=uuid.uuid4(),
            workspace_id=None,
            provider="anthropic",
            model="claude-sonnet-4-5",
            kind="completion",
            prompt_usd_per_1k=Decimal("0.003"),
            completion_usd_per_1k=Decimal("0.015"),
            effective_from=NOW - timedelta(days=30),
        )
    )
    return store


@pytest.fixture
def audit_sink() -> CapturingAuditSink:
    return CapturingAuditSink()


@pytest.fixture
def cost_service(ledger, price_book, price_store, audit_sink) -> CostService:
    return CostService(
        reader=ledger,
        ledger=ledger,
        price_book=price_book,
        prices=price_store,
        scopes=InMemoryScopeResolver(
            {TASK_ID: WS_ID, PROJECT_ID: WS_ID, FOREIGN_TASK_ID: OTHER_WS_ID}
        ),
        audit=audit_sink,
    )


@pytest.fixture
def client_factory(cost_service: CostService) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def _build(
        role: UserRole = UserRole.ADMIN,
        *,
        workspace_id: uuid.UUID = WS_ID,
        authenticated: bool = True,
    ) -> TestClient:
        app = create_app()
        app.dependency_overrides[get_cost_service] = lambda: cost_service
        if authenticated:
            principal = Principal(
                user_id=uuid.uuid4(),
                workspace_id=workspace_id,
                role=role,
                email="cost-test@forge.local",
                auth_method="test",
                scopes=["*"],
            )
            app.dependency_overrides[get_current_principal] = lambda: principal
        tc = TestClient(app)
        clients.append(tc)
        return tc

    yield _build
    for tc in clients:
        tc.close()


__all__ = [
    "FOREIGN_TASK_ID",
    "NOW",
    "OTHER_WS_ID",
    "PROJECT_ID",
    "TASK_ID",
    "WS_ID",
    "CapturingAuditSink",
    "audit_sink",
    "client_factory",
    "cost_service",
    "ledger",
    "price_book",
    "price_store",
]
