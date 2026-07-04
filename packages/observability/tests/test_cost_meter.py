"""UsageMeter — the single emission point (F38 AC5/AC6/AC7)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from forge_obs.cost.meter import NoopUsageMeter, UsageMeter
from forge_obs.cost.models import ModelPrice, ModelUsage
from forge_obs.cost.pricing import InMemoryPriceBook
from forge_obs.cost.repository import InMemoryCostLedger
from forge_obs.metrics import RecordingMetrics

WS = uuid.uuid4()
NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


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
                effective_from=NOW - timedelta(days=10),
            )
        ]
    )


@pytest.fixture
def ledger() -> InMemoryCostLedger:
    return InMemoryCostLedger()


@pytest.fixture
def metrics() -> RecordingMetrics:
    return RecordingMetrics(service="forge-worker")


@pytest.fixture
def meter(ledger, price_book, metrics) -> UsageMeter:
    return UsageMeter(ledger=ledger, price_book=price_book, metrics=metrics)


def _usage(request_id: str = "req-1", model: str = "claude-sonnet-4-5") -> ModelUsage:
    return ModelUsage(
        workspace_id=WS,
        request_id=request_id,
        provider="anthropic",
        model=model,
        kind="completion",
        prompt_tokens=2000,
        completion_tokens=500,
        occurred_at=NOW,
        phase="executing",
    )


def test_record_inserts_event_and_increments_counters(meter, ledger, metrics) -> None:
    record = meter.record(_usage())
    assert record.cost_usd == Decimal("0.0135")
    assert record.priced is True
    assert record.cost_event_id is not None
    assert len(ledger.rows()) == 1
    assert metrics.counter_value("forge_model_tokens_total", token_kind="prompt") == 2000
    assert metrics.counter_value("forge_model_tokens_total", token_kind="completion") == 500
    assert metrics.counter_value("forge_model_cost_usd_total") == pytest.approx(0.0135)


def test_record_idempotent_on_request_id(meter, ledger, metrics) -> None:
    """AC5: a retried record is a no-op — one row, no double count."""
    first = meter.record(_usage())
    second = meter.record(_usage())
    assert second.deduplicated is True
    assert second.cost_event_id == first.cost_event_id
    assert second.cost_usd == first.cost_usd
    assert len(ledger.rows()) == 1
    assert metrics.counter_value("forge_model_cost_usd_total") == pytest.approx(0.0135)
    assert metrics.counter_value("forge_model_tokens_total", token_kind="prompt") == 2000


def test_unpriced_model_records_zero_and_warns(meter, ledger, metrics) -> None:
    """AC6: gap is visible (counter), call is not dropped (row persists)."""
    record = meter.record(_usage(request_id="req-2", model="mystery-model"))
    assert record.cost_usd == Decimal(0)
    assert record.priced is False
    assert len(ledger.rows()) == 1
    assert (
        metrics.counter_value(
            "forge_unpriced_model_total", provider="anthropic", model="mystery-model"
        )
        == 1
    )


class _ExplodingMetrics(RecordingMetrics):
    def record_model_cost(self, **kwargs) -> None:
        raise RuntimeError("exporter down")


def test_metric_export_failure_is_swallowed(ledger, price_book) -> None:
    """AC7: the ledger row still lands; no exception propagates."""
    meter = UsageMeter(
        ledger=ledger, price_book=price_book, metrics=_ExplodingMetrics(service="t")
    )
    record = meter.record(_usage())
    assert record.cost_usd == Decimal("0.0135")
    assert len(ledger.rows()) == 1


class _ExplodingLedger(InMemoryCostLedger):
    def upsert_event(self, usage, *, cost, price_id):
        raise RuntimeError("db down")


def test_ledger_failure_increments_failure_counter_non_strict(price_book, metrics) -> None:
    meter = UsageMeter(ledger=_ExplodingLedger(), price_book=price_book, metrics=metrics)
    record = meter.record(_usage())
    assert record.cost_event_id is None
    assert record.cost_usd == Decimal("0.0135")  # cost still computed for the step stamp
    assert metrics.counter_value("forge_cost_emit_failures_total", reason="ledger") == 1


def test_ledger_failure_raises_in_strict_mode(price_book, metrics) -> None:
    meter = UsageMeter(
        ledger=_ExplodingLedger(), price_book=price_book, metrics=metrics, strict=True
    )
    with pytest.raises(RuntimeError, match="db down"):
        meter.record(_usage())
    assert metrics.counter_value("forge_cost_emit_failures_total", reason="ledger") == 1


def test_unknown_kind_rejected(meter) -> None:
    bad = _usage().model_copy(update={"kind": "video"})
    with pytest.raises(ValueError, match="unknown cost kind"):
        meter.record(bad)


def test_noop_meter_returns_empty_record() -> None:
    record = NoopUsageMeter().record(_usage())
    assert record.cost_event_id is None and record.cost_usd == Decimal(0)
