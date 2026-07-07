"""In-memory ledger rollups + reprice (F38 AC9/AC10/AC15 shapes)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from forge_obs.cost.models import ModelPrice, ModelUsage
from forge_obs.cost.pricing import InMemoryPriceBook
from forge_obs.cost.repository import InMemoryCostLedger

WS = uuid.uuid4()
PROJECT = uuid.uuid4()
TASK = uuid.uuid4()
DAY0 = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)


def _seed(ledger: InMemoryCostLedger) -> None:
    rows = [
        # (request, provider, model, phase, occurred, cost)
        ("r1", "anthropic", "sonnet", "spec_drafting", DAY0, "0.04"),
        ("r2", "anthropic", "sonnet", "executing", DAY0 + timedelta(hours=2), "0.28"),
        ("r3", "openai", "embed", "executing", DAY0 + timedelta(days=1), "0.06"),
        ("r4", "anthropic", "sonnet", "verifying", DAY0 + timedelta(days=1, hours=3), "0.05"),
    ]
    for request_id, provider, model, phase, occurred, cost in rows:
        ledger.upsert_event(
            ModelUsage(
                workspace_id=WS,
                request_id=request_id,
                provider=provider,
                model=model,
                kind="completion",
                prompt_tokens=1000,
                completion_tokens=100,
                occurred_at=occurred,
                project_id=PROJECT,
                task_id=TASK,
                phase=phase,
            ),
            cost=Decimal(cost),
            price_id=None,
        )


@pytest.fixture
def ledger() -> InMemoryCostLedger:
    ledger = InMemoryCostLedger()
    _seed(ledger)
    return ledger


@pytest.mark.parametrize("group_by", ["phase", "provider", "model", "none"])
def test_summary_bucket_sums_match_total(ledger, group_by) -> None:
    summary = ledger.summary(
        workspace_id=WS, scope="task", scope_id=TASK, group_by=group_by, frm=None, to=None
    )
    assert summary.total_cost_usd == Decimal("0.43")
    assert sum(b.cost_usd for b in summary.buckets) == summary.total_cost_usd
    assert summary.total_prompt_tokens == 4000
    assert summary.total_completion_tokens == 400


def test_summary_by_phase_buckets(ledger) -> None:
    summary = ledger.summary(
        workspace_id=WS, scope="task", scope_id=TASK, group_by="phase", frm=None, to=None
    )
    by_key = {b.key: b.cost_usd for b in summary.buckets}
    assert by_key == {
        "spec_drafting": Decimal("0.04"),
        "executing": Decimal("0.34"),
        "verifying": Decimal("0.05"),
    }


def test_summary_scope_isolation(ledger) -> None:
    other = ledger.summary(
        workspace_id=uuid.uuid4(),
        scope="task",
        scope_id=TASK,
        group_by="none",
        frm=None,
        to=None,
    )
    assert other.total_cost_usd == Decimal(0) and other.buckets == []


def test_summary_time_window(ledger) -> None:
    only_day0 = ledger.summary(
        workspace_id=WS,
        scope="project",
        scope_id=PROJECT,
        group_by="none",
        frm=DAY0,
        to=DAY0 + timedelta(days=1),
    )
    assert only_day0.total_cost_usd == Decimal("0.32")


def test_timeseries_buckets_sum_to_total(ledger) -> None:
    ts = ledger.timeseries(
        workspace_id=WS,
        scope="project",
        scope_id=PROJECT,
        bucket="day",
        group_by="provider",
        frm=None,
        to=None,
    )
    assert set(ts.series) == {"anthropic", "openai"}
    total = sum(cost for points in ts.series.values() for _, cost in points)
    assert total == Decimal("0.43")
    anthropic_days = dict(ts.series["anthropic"])
    assert anthropic_days == {
        DAY0.replace(hour=0): Decimal("0.32"),
        (DAY0 + timedelta(days=1)).replace(hour=0): Decimal("0.05"),
    }


def test_timeseries_rejects_bad_bucket(ledger) -> None:
    with pytest.raises(ValueError, match="unknown bucket"):
        ledger.timeseries(
            workspace_id=WS,
            scope="task",
            scope_id=TASK,
            bucket="fortnight",
            group_by="none",
            frm=None,
            to=None,
        )


def test_reprice_only_affected_rows_idempotent(ledger) -> None:
    """AC15: only rows since the date whose (provider,model) price changed."""
    book = InMemoryPriceBook(
        [
            ModelPrice(
                id=uuid.uuid4(),
                provider="anthropic",
                model="sonnet",
                kind="completion",
                prompt_usd_per_1k=Decimal("0.001"),
                completion_usd_per_1k=Decimal("0.001"),
                effective_from=DAY0 - timedelta(days=1),
            )
        ]
    )
    since = DAY0 + timedelta(days=1)
    updated = ledger.reprice(
        workspace_id=WS, since=since, provider="anthropic", model="sonnet", price_book=book
    )
    assert updated == 1  # only r4 (anthropic, on/after since)

    # New cost: 1000/1k*0.001 + 100/1k*0.001 = 0.0011
    summary = ledger.summary(
        workspace_id=WS, scope="task", scope_id=TASK, group_by="provider", frm=None, to=None
    )
    by_key = {b.key: b.cost_usd for b in summary.buckets}
    assert by_key["anthropic"] == Decimal("0.04") + Decimal("0.28") + Decimal("0.0011")
    assert by_key["openai"] == Decimal("0.06")  # untouched (provider filter)

    # Idempotent: re-running changes nothing further.
    assert (
        ledger.reprice(
            workspace_id=WS, since=since, provider="anthropic", model="sonnet", price_book=book
        )
        == 0
    )
