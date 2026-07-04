"""HARD-10 — worker telemetry init + the process UsageMeter (hermetic)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from forge_obs.cost.models import ModelPrice, ModelUsage
from forge_obs.cost.pricing import InMemoryPriceBook
from forge_obs.cost.repository import InMemoryCostLedger
from forge_obs.metrics import NoopMetrics, RecordingMetrics, reset_metrics
from forge_obs.settings import ObsSettings
from forge_obs.telemetry import shutdown_telemetry
from forge_worker.observability import build_usage_meter, setup_worker_telemetry


@pytest.fixture(autouse=True)
def _isolate():
    yield
    shutdown_telemetry()
    reset_metrics()


def test_setup_worker_telemetry_disabled_is_noop() -> None:
    handle = setup_worker_telemetry(ObsSettings(enabled=False))
    assert handle.service_name == "forge-worker"
    assert isinstance(handle.metrics, NoopMetrics)
    assert handle.exporting is False


def test_setup_worker_telemetry_enabled_records() -> None:
    handle = setup_worker_telemetry(ObsSettings(enabled=True))
    assert isinstance(handle.metrics, RecordingMetrics)


def test_build_usage_meter_records_cost_through_injected_ledger() -> None:
    ws = uuid.uuid4()
    metrics = RecordingMetrics(service="forge-worker")
    price_book = InMemoryPriceBook(
        [
            ModelPrice(
                id=uuid.uuid4(),
                provider="anthropic",
                model="claude-sonnet-4-5",
                kind="completion",
                prompt_usd_per_1k=3,
                completion_usd_per_1k=15,
                effective_from=datetime(2026, 1, 1, tzinfo=UTC),
            )
        ]
    )
    ledger = InMemoryCostLedger()
    meter = build_usage_meter(ledger=ledger, price_book=price_book, metrics=metrics)

    usage = ModelUsage(
        workspace_id=ws,
        request_id="req-1",
        provider="anthropic",
        model="claude-sonnet-4-5",
        kind="completion",
        prompt_tokens=1000,
        completion_tokens=1000,
        occurred_at=datetime(2026, 6, 1, tzinfo=UTC),
        phase="executing",
    )
    record = meter.record(usage)
    # (1000/1000)*3 + (1000/1000)*15 = 18 USD.
    assert record.cost_usd == pytest.approx(18)
    assert record.priced is True
    assert len(ledger.rows()) == 1
    cost_total = metrics.counter_value("forge_model_cost_usd_total", provider="anthropic")
    assert cost_total == pytest.approx(18)
    # Idempotent replay: no second row, no double-count.
    again = meter.record(usage)
    assert again.deduplicated is True
    assert len(ledger.rows()) == 1
    cost_total_after = metrics.counter_value("forge_model_cost_usd_total", provider="anthropic")
    assert cost_total_after == pytest.approx(18)
