"""Postgres integration: SqlCostLedger / SqlCostReader / DbPriceBook (F38).

End-to-end over the real ``cost_event``/``model_price`` tables: idempotent
upsert (unique index), price resolution with workspace override, rollups whose
bucket sums equal the total, reprice, and the ledger/counter agreement (AC5,
AC8 core, AC9/AC10, AC15). Parks without Postgres (shared ``pg_engine``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from forge_db.base import Base
from forge_db.models import CostEvent, Project, Task, Workspace
from forge_db.models import ModelPrice as ModelPriceRow
from forge_obs.cost.meter import UsageMeter
from forge_obs.cost.models import ModelUsage
from forge_obs.cost.pricing import DbPriceBook
from forge_obs.cost.repository import SqlCostLedger, SqlCostReader
from forge_obs.metrics import RecordingMetrics

pytestmark = pytest.mark.usefixtures("pg_engine")

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def factory(pg_engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    try:
        yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(pg_engine)


@pytest.fixture
def seeded(factory) -> dict:
    """Workspace + project + task + a global and an override price row."""
    with factory() as session:
        ws = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
        other = Workspace(name="Rival", slug=f"rival-{uuid.uuid4().hex[:8]}")
        session.add_all([ws, other])
        session.flush()
        project = Project(workspace_id=ws.id, name="Forge", key=f"FRG{uuid.uuid4().hex[:4]}")
        session.add(project)
        session.flush()
        task = Task(
            workspace_id=ws.id,
            project_id=project.id,
            key=f"FRG-{uuid.uuid4().hex[:6]}",
            title="Do the thing",
        )
        session.add(task)
        session.flush()
        global_price = ModelPriceRow(
            workspace_id=None,
            provider="anthropic",
            model="claude-sonnet-4-5",
            kind="completion",
            prompt_usd_per_1k=Decimal("0.003"),
            completion_usd_per_1k=Decimal("0.015"),
            effective_from=NOW - timedelta(days=30),
        )
        override = ModelPriceRow(
            workspace_id=ws.id,
            provider="anthropic",
            model="claude-sonnet-4-5",
            kind="completion",
            prompt_usd_per_1k=Decimal("0.001"),
            completion_usd_per_1k=Decimal("0.005"),
            effective_from=NOW - timedelta(days=5),
        )
        session.add_all([global_price, override])
        session.commit()
        return {
            "ws": ws.id,
            "other_ws": other.id,
            "project": project.id,
            "task": task.id,
            "global_price": global_price.id,
            "override": override.id,
        }


def _usage(seeded: dict, request_id: str, **overrides) -> ModelUsage:
    defaults = {
        "workspace_id": seeded["ws"],
        "request_id": request_id,
        "provider": "anthropic",
        "model": "claude-sonnet-4-5",
        "kind": "completion",
        "prompt_tokens": 2000,
        "completion_tokens": 500,
        "occurred_at": NOW,
        "project_id": seeded["project"],
        "task_id": seeded["task"],
        "phase": "executing",
    }
    defaults.update(overrides)
    return ModelUsage(**defaults)


def test_db_price_book_prefers_workspace_override_and_dates(factory, seeded) -> None:
    book = DbPriceBook(factory)
    resolved = book.resolve(
        workspace_id=seeded["ws"], provider="anthropic", model="claude-sonnet-4-5",
        kind="completion", at=NOW,
    )
    assert resolved is not None and resolved.id == seeded["override"]

    # Before the override's effective_from the global row was in force.
    older = book.resolve(
        workspace_id=seeded["ws"], provider="anthropic", model="claude-sonnet-4-5",
        kind="completion", at=NOW - timedelta(days=10),
    )
    assert older is not None and older.id == seeded["global_price"]

    # Another workspace only sees the global default.
    other = book.resolve(
        workspace_id=seeded["other_ws"], provider="anthropic", model="claude-sonnet-4-5",
        kind="completion", at=NOW,
    )
    assert other is not None and other.id == seeded["global_price"]


def test_meter_end_to_end_ledger_counters_and_generated_column(factory, seeded) -> None:
    """AC5 + AC8 core: one row, counters match ledger, total_tokens generated."""
    metrics = RecordingMetrics(service="forge-worker")
    meter = UsageMeter(
        ledger=SqlCostLedger(factory), price_book=DbPriceBook(factory), metrics=metrics
    )
    record = meter.record(_usage(seeded, "req-e2e"))
    # Override price: 2000/1k*0.001 + 500/1k*0.005 = 0.0045
    assert record.cost_usd == Decimal("0.0045")
    assert record.priced is True and record.price_id == seeded["override"]

    replay = meter.record(_usage(seeded, "req-e2e"))
    assert replay.deduplicated is True and replay.cost_event_id == record.cost_event_id

    with factory() as session:
        rows = session.scalars(select(CostEvent)).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.total_tokens == 2500  # generated column
        assert Decimal(row.cost_usd) == Decimal("0.0045")
        assert row.price_id == seeded["override"]

    # Ledger total == Prometheus counter total (no double count on replay).
    assert metrics.counter_value("forge_model_cost_usd_total") == pytest.approx(0.0045)
    assert metrics.counter_value("forge_model_tokens_total", token_kind="prompt") == 2000


def test_sql_reader_summary_and_timeseries_agree(factory, seeded) -> None:
    ledger = SqlCostLedger(factory)
    ledger.upsert_event(
        _usage(seeded, "r1", phase="spec_drafting"), cost=Decimal("0.04"), price_id=None
    )
    ledger.upsert_event(_usage(seeded, "r2"), cost=Decimal("0.28"), price_id=None)
    ledger.upsert_event(
        _usage(
            seeded, "r3", provider="openai", model="text-embedding-3-small",
            kind="embedding", occurred_at=NOW + timedelta(days=1),
        ),
        cost=Decimal("0.06"),
        price_id=None,
    )

    reader = SqlCostReader(factory)
    summary = reader.summary(
        workspace_id=seeded["ws"], scope="task", scope_id=seeded["task"],
        group_by="provider", frm=None, to=None,
    )
    assert summary.total_cost_usd == Decimal("0.38")
    by_key = {b.key: b.cost_usd for b in summary.buckets}
    assert by_key == {"anthropic": Decimal("0.32"), "openai": Decimal("0.06")}
    assert sum(b.cost_usd for b in summary.buckets) == summary.total_cost_usd

    by_phase = reader.summary(
        workspace_id=seeded["ws"], scope="project", scope_id=seeded["project"],
        group_by="phase", frm=None, to=None,
    )
    phase_keys = {b.key: b.cost_usd for b in by_phase.buckets}
    assert phase_keys == {"spec_drafting": Decimal("0.04"), "executing": Decimal("0.34")}

    ts = reader.timeseries(
        workspace_id=seeded["ws"], scope="workspace", scope_id=seeded["ws"],
        bucket="day", group_by="provider", frm=None, to=None,
    )
    total = sum(cost for points in ts.series.values() for _, cost in points)
    assert total == Decimal("0.38")

    # Cross-workspace read returns nothing (isolation at the repository floor).
    empty = reader.summary(
        workspace_id=seeded["other_ws"], scope="task", scope_id=seeded["task"],
        group_by="none", frm=None, to=None,
    )
    assert empty.total_cost_usd == Decimal(0)


def test_sql_reprice_updates_only_changed_rows(factory, seeded) -> None:
    """AC15: recompute cost_usd from the price in force; idempotent."""
    ledger = SqlCostLedger(factory)
    book = DbPriceBook(factory)
    meter = UsageMeter(ledger=ledger, price_book=book, metrics=None)
    meter.record(_usage(seeded, "r-reprice"))

    # Admin fixes the override price retroactively.
    with factory() as session:
        session.add(
            ModelPriceRow(
                workspace_id=seeded["ws"],
                provider="anthropic",
                model="claude-sonnet-4-5",
                kind="completion",
                prompt_usd_per_1k=Decimal("0.002"),
                completion_usd_per_1k=Decimal("0.010"),
                effective_from=NOW - timedelta(days=1),
            )
        )
        session.commit()

    updated = ledger.reprice(
        workspace_id=seeded["ws"], since=NOW - timedelta(days=2),
        provider=None, model=None, price_book=book,
    )
    assert updated == 1
    with factory() as session:
        row = session.scalars(select(CostEvent)).one()
        # 2000/1k*0.002 + 500/1k*0.010 = 0.009
        assert Decimal(row.cost_usd) == Decimal("0.009")

    assert (
        ledger.reprice(
            workspace_id=seeded["ws"], since=NOW - timedelta(days=2),
            provider=None, model=None, price_book=book,
        )
        == 0
    )
