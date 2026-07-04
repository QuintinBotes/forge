"""F38 worker tasks: cost.reprice + obs.refresh_freshness_gauges (hermetic)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from forge_db.base import Base
from forge_db.models import AuditLog, KnowledgeSource, MCPConnection, Workspace
from forge_db.models.cost import CostEvent
from forge_db.models.cost import ModelPrice as ModelPriceRow
from forge_db.models.enums import CostEventKind, KnowledgeSourceKind
from forge_obs.cost.models import ModelUsage
from forge_obs.cost.repository import SqlCostLedger
from forge_obs.metrics import RecordingMetrics
from forge_worker.beat import BEAT_SCHEDULE, OBS_FRESHNESS_TASK
from forge_worker.celery_app import celery_app
from forge_worker.tasks.observability import (
    FRESHNESS_TASK,
    REPRICE_TASK,
    run_refresh_freshness,
    run_reprice,
)

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    finally:
        engine.dispose()


@pytest.fixture
def workspace_id(factory) -> uuid.UUID:
    with factory() as session:
        ws = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
        session.add(ws)
        session.commit()
        return ws.id


def test_tasks_registered_and_beat_scheduled() -> None:
    assert REPRICE_TASK in celery_app.tasks
    assert FRESHNESS_TASK in celery_app.tasks
    entry = BEAT_SCHEDULE["obs-refresh-freshness-gauges"]
    assert entry["task"] == OBS_FRESHNESS_TASK == FRESHNESS_TASK


def test_run_reprice_updates_rows_and_audits(factory, workspace_id) -> None:
    ledger = SqlCostLedger(factory)
    ledger.upsert_event(
        ModelUsage(
            workspace_id=workspace_id,
            request_id="req-1",
            provider="anthropic",
            model="claude-sonnet-4-5",
            kind="completion",
            prompt_tokens=1000,
            completion_tokens=100,
            occurred_at=NOW,
            phase="executing",
        ),
        cost=Decimal("0.5"),
        price_id=None,
    )
    with factory() as session:
        session.add(
            ModelPriceRow(
                workspace_id=None,
                provider="anthropic",
                model="claude-sonnet-4-5",
                kind=CostEventKind.COMPLETION,
                prompt_usd_per_1k=Decimal("0.001"),
                completion_usd_per_1k=Decimal("0.001"),
                effective_from=NOW - timedelta(days=1),
            )
        )
        session.commit()

    updated = run_reprice(
        factory, workspace_id=workspace_id, since=NOW - timedelta(days=1)
    )
    assert updated == 1
    with factory() as session:
        row = session.scalars(select(CostEvent)).one()
        assert Decimal(str(row.cost_usd)) == Decimal("0.0011")
        audit = session.scalars(
            select(AuditLog).where(AuditLog.action == "cost.repriced")
        ).one()
        assert audit.workspace_id == workspace_id
        assert audit.details["updated"] == 1

    # Idempotent: nothing left to change on a re-run.
    assert run_reprice(factory, workspace_id=workspace_id, since=NOW - timedelta(days=1)) == 0


def test_run_refresh_freshness_sets_gauge_per_connection(factory, workspace_id) -> None:
    with factory() as session:
        conn = MCPConnection(workspace_id=workspace_id, slug="github", name="GitHub")
        session.add(conn)
        session.flush()
        session.add(
            KnowledgeSource(
                workspace_id=workspace_id,
                kind=KnowledgeSourceKind.MCP,
                name="GitHub issues",
                uri="mcp://github/issues",
                mcp_connection_id=conn.id,
                last_synced_at=NOW - timedelta(seconds=120),
            )
        )
        # A never-synced source must not produce a gauge.
        session.add(
            KnowledgeSource(
                workspace_id=workspace_id,
                kind=KnowledgeSourceKind.MCP,
                name="Never synced",
                uri="mcp://github/wiki",
                mcp_connection_id=conn.id,
                last_synced_at=None,
            )
        )
        session.commit()

    metrics = RecordingMetrics(service="forge-worker")
    lags = run_refresh_freshness(factory, metrics=metrics, now=NOW)
    assert lags == {"github": pytest.approx(120.0)}
    assert metrics.gauge_value(
        "forge_mcp_freshness_lag_seconds", connection="github"
    ) == pytest.approx(120.0)


def test_run_refresh_freshness_empty_is_noop(factory, workspace_id) -> None:
    metrics = RecordingMetrics(service="forge-worker")
    assert run_refresh_freshness(factory, metrics=metrics, now=NOW) == {}
    assert metrics.gauges.get("forge_mcp_freshness_lag_seconds") is None
