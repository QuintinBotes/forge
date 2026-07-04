"""compute_cost exactness + price resolution rules (F38 AC3/AC4)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from forge_obs.cost.models import ModelPrice, ModelUsage
from forge_obs.cost.pricing import InMemoryPriceBook, compute_cost

WS = uuid.uuid4()
NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def _usage(prompt: int = 2000, completion: int = 500) -> ModelUsage:
    return ModelUsage(
        workspace_id=WS,
        request_id="req-1",
        provider="anthropic",
        model="claude-sonnet-4-5",
        prompt_tokens=prompt,
        completion_tokens=completion,
        occurred_at=NOW,
    )


def _price(
    prompt: str,
    completion: str,
    *,
    workspace_id: uuid.UUID | None = None,
    effective_from: datetime = NOW - timedelta(days=30),
) -> ModelPrice:
    return ModelPrice(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        provider="anthropic",
        model="claude-sonnet-4-5",
        kind="completion",
        prompt_usd_per_1k=Decimal(prompt),
        completion_usd_per_1k=Decimal(completion),
        effective_from=effective_from,
    )


def test_compute_cost_exact() -> None:
    """AC3: 2000 prompt @ 0.003/1k + 500 completion @ 0.015/1k == 0.0135."""
    assert compute_cost(_usage(), _price("0.003", "0.015")) == Decimal("0.0135")


def test_compute_cost_none_price_is_zero() -> None:
    assert compute_cost(_usage(), None) == Decimal(0)


def test_compute_cost_zero_tokens_is_zero() -> None:
    assert compute_cost(_usage(0, 0), _price("0.003", "0.015")) == Decimal(0)


def test_price_resolution_prefers_workspace_override() -> None:
    book = InMemoryPriceBook()
    global_price = _price("0.003", "0.015")
    override = _price("0.001", "0.005", workspace_id=WS)
    book.add(global_price)
    book.add(override)
    resolved = book.resolve(
        workspace_id=WS, provider="anthropic", model="claude-sonnet-4-5",
        kind="completion", at=NOW,
    )
    assert resolved is not None and resolved.id == override.id

    other_ws = book.resolve(
        workspace_id=uuid.uuid4(), provider="anthropic", model="claude-sonnet-4-5",
        kind="completion", at=NOW,
    )
    assert other_ws is not None and other_ws.id == global_price.id


def test_price_resolution_uses_price_in_force_at_occurred_at() -> None:
    """AC4: newest effective_from <= occurred_at wins; future prices don't."""
    book = InMemoryPriceBook()
    old = _price("0.003", "0.015", effective_from=NOW - timedelta(days=60))
    new = _price("0.004", "0.020", effective_from=NOW - timedelta(days=1))
    future = _price("0.009", "0.090", effective_from=NOW + timedelta(days=1))
    for p in (old, new, future):
        book.add(p)

    at_now = book.resolve(
        workspace_id=WS, provider="anthropic", model="claude-sonnet-4-5",
        kind="completion", at=NOW,
    )
    assert at_now is not None and at_now.id == new.id

    back_then = book.resolve(
        workspace_id=WS, provider="anthropic", model="claude-sonnet-4-5",
        kind="completion", at=NOW - timedelta(days=30),
    )
    assert back_then is not None and back_then.id == old.id


def test_price_resolution_unknown_model_is_none() -> None:
    book = InMemoryPriceBook([_price("0.003", "0.015")])
    assert (
        book.resolve(
            workspace_id=WS, provider="anthropic", model="unknown-model",
            kind="completion", at=NOW,
        )
        is None
    )
