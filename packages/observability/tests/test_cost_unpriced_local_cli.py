"""Local-CLI runs are unpriced, never free (F38; local-cli-provider Task 9).

A locally-driven CLI (``claude``/``codex`` over a subprocess or the loopback
bridge) consumes a paid *subscription*, not a metered API call — it has real
cost and no per-token price. If ``local-cli:claude`` ever matched an Anthropic
pricing row (by exact string, prefix, or substring), a subscription run would
get billed at API rates and silently inflate every cost-per-delivered-change
figure downstream. Conversely, if an unpriced call were recorded as
``cost_usd=0`` without a distinguishing marker, local runs would look free.

``ProviderName.local_cli`` (``packages/agent-runtime/forge_agent/providers/config.py``)
namespaces the model string as ``local-cli:<cli>`` (``local-cli:claude``,
``local-cli:codex``), falling back to the bare ``local-cli`` when no CLI name
is known. ``PriceBook.resolve`` (``forge_obs/cost/pricing.py``) already matches
``provider``/``model``/``kind`` by exact equality — no prefix or substring
matching — so these namespaced strings can never collide with a real pricing
row by construction. These tests pin that behaviour end to end: resolution,
``compute_cost``, and ``UsageMeter.record``'s ``CostRecord.priced`` marker.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from forge_obs.cost.meter import UsageMeter
from forge_obs.cost.models import ModelPrice, ModelUsage
from forge_obs.cost.pricing import InMemoryPriceBook, compute_cost
from forge_obs.cost.repository import InMemoryCostLedger
from forge_obs.metrics import RecordingMetrics

WS = uuid.uuid4()
NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)

#: The real local-CLI model strings (Amendment A) — the namespaced pair plus
#: the bare fallback used when no CLI name was detected.
LOCAL_CLI_MODELS = ("local-cli:claude", "local-cli:codex", "local-cli")


@pytest.fixture
def anthropic_priced_book() -> InMemoryPriceBook:
    """A price book with a real, currently-priced Anthropic model.

    Exists so the local-cli tests below run against a book that could plausibly
    mis-match by prefix/substring if resolution were ever loosened — pinning
    that it does not.
    """
    return InMemoryPriceBook(
        [
            ModelPrice(
                id=uuid.uuid4(),
                provider="anthropic",
                model="claude-opus-4-8",
                kind="completion",
                prompt_usd_per_1k=Decimal("0.015"),
                completion_usd_per_1k=Decimal("0.075"),
                effective_from=NOW - timedelta(days=30),
            )
        ]
    )


@pytest.mark.parametrize("model", LOCAL_CLI_MODELS)
def test_local_cli_model_never_resolves_a_price(
    anthropic_priced_book: InMemoryPriceBook, model: str
) -> None:
    """Exact-match resolution: a namespaced local-cli model never matches

    an unrelated provider's pricing row, even when one shares the substring
    ``claude`` and even when the local-cli provider itself has no rows at all.
    """
    assert (
        anthropic_priced_book.resolve(
            workspace_id=WS,
            provider="local_cli",
            model=model,
            kind="completion",
            at=NOW,
        )
        is None
    )


def test_unknown_model_is_also_unpriced() -> None:
    """Same guarantee for any model the price book has never seen (not just local-cli)."""
    book = InMemoryPriceBook()
    assert (
        book.resolve(
            workspace_id=WS,
            provider="anthropic",
            model="some-model-we-have-never-seen",
            kind="completion",
            at=NOW,
        )
        is None
    )


@pytest.mark.parametrize("model", LOCAL_CLI_MODELS)
def test_compute_cost_for_unresolved_local_cli_price_is_zero_not_billed(model: str) -> None:
    """``compute_cost(usage, None)`` is the zero *arithmetic* result; the caller

    (``UsageMeter.record``) is what marks the row ``priced=False`` so reporting
    can tell this apart from a genuinely free/zero-token call.
    """
    usage = ModelUsage(
        workspace_id=WS,
        request_id="req-1",
        provider="local_cli",
        model=model,
        prompt_tokens=4000,
        completion_tokens=1200,
        occurred_at=NOW,
    )
    assert compute_cost(usage, None) == Decimal(0)


@pytest.mark.parametrize("model", LOCAL_CLI_MODELS)
def test_local_cli_run_is_recorded_unpriced_with_volume_preserved(
    anthropic_priced_book: InMemoryPriceBook, model: str
) -> None:
    """End-to-end through ``UsageMeter.record``: the run lands in the ledger with

    real token volume, ``priced=False`` (never silently free), and the
    ``forge_unpriced_model_total`` gap counter increments — a subscription run
    is visible, not billed and not dropped.
    """
    ledger = InMemoryCostLedger()
    metrics = RecordingMetrics(service="forge-worker")
    meter = UsageMeter(ledger=ledger, price_book=anthropic_priced_book, metrics=metrics)

    usage = ModelUsage(
        workspace_id=WS,
        request_id=f"req-{model}",
        provider="local_cli",
        model=model,
        prompt_tokens=4000,
        completion_tokens=1200,
        occurred_at=NOW,
        phase="executing",
    )
    record = meter.record(usage)

    # Never billed at API rates, and never silently free: ``priced`` is the
    # explicit marker a report must check instead of trusting ``cost_usd``.
    assert record.cost_usd == Decimal(0)
    assert record.priced is False
    assert record.cost_event_id is not None

    # Token volume the CLI reported is still persisted for reporting, even
    # though no monetary figure was computed.
    rows = ledger.rows()
    assert len(rows) == 1
    assert rows[0]["prompt_tokens"] == 4000
    assert rows[0]["completion_tokens"] == 1200
    assert rows[0]["price_id"] is None

    assert (
        metrics.counter_value("forge_unpriced_model_total", provider="local_cli", model=model) == 1
    )
