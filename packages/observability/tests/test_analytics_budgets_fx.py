"""F40-OBS-ANALYTICS: budget hard-cap alerts + multi-currency FX conversion."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from forge_obs.analytics.budgets import Budget, evaluate_budget
from forge_obs.analytics.fx import FxRate, InMemoryFxRateBook, convert

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def test_convert_same_currency_is_exact_and_lookup_free() -> None:
    book = InMemoryFxRateBook()
    assert convert(Decimal("42.00"), base="USD", quote="USD", at=NOW, book=book) == Decimal("42.00")


def test_convert_resolves_direct_and_falls_back_to_inverse_rate() -> None:
    book = InMemoryFxRateBook(
        [FxRate(base_currency="USD", quote_currency="EUR", rate=Decimal("0.9"), effective_from=NOW)]
    )
    assert convert(Decimal("100"), base="USD", quote="EUR", at=NOW, book=book) == Decimal("90.0")
    # No EUR->USD row directly, but the USD->EUR row's inverse resolves it.
    inverse = convert(Decimal("90"), base="EUR", quote="USD", at=NOW, book=book)
    assert inverse == pytest.approx(Decimal("100"))


def test_convert_returns_none_when_no_rate_in_force() -> None:
    book = InMemoryFxRateBook()
    assert convert(Decimal("1"), base="USD", quote="GBP", at=NOW, book=book) is None


def test_convert_picks_newest_effective_rate() -> None:
    book = InMemoryFxRateBook(
        [
            FxRate(
                base_currency="USD",
                quote_currency="EUR",
                rate=Decimal("0.8"),
                effective_from=NOW - timedelta(days=30),
            ),
            FxRate(
                base_currency="USD", quote_currency="EUR", rate=Decimal("0.9"), effective_from=NOW
            ),
        ]
    )
    assert convert(Decimal("10"), base="USD", quote="EUR", at=NOW, book=book) == Decimal("9.0")


def _budget(**overrides) -> Budget:
    defaults = {
        "workspace_id": uuid.uuid4(),
        "name": "Monthly cap",
        "amount": Decimal("1000"),
        "currency": "USD",
    }
    defaults.update(overrides)
    return Budget(**defaults)


def test_hard_cap_budget_alerts_when_spend_exceeds_amount() -> None:
    budget = _budget(hard_cap=True)
    status = evaluate_budget(budget, Decimal("1200"), fx=InMemoryFxRateBook(), at=NOW)
    assert status.over_budget is True
    assert status.alert is True
    assert status.pct_used == pytest.approx(1.2)


def test_soft_cap_budget_over_amount_does_not_alert() -> None:
    budget = _budget(hard_cap=False)
    status = evaluate_budget(budget, Decimal("1200"), fx=InMemoryFxRateBook(), at=NOW)
    assert status.over_budget is True
    assert status.alert is False


def test_under_budget_never_alerts() -> None:
    budget = _budget(hard_cap=True)
    status = evaluate_budget(budget, Decimal("500"), fx=InMemoryFxRateBook(), at=NOW)
    assert status.over_budget is False
    assert status.alert is False
    assert status.pct_used == pytest.approx(0.5)


def test_non_usd_budget_converts_spend_before_comparing() -> None:
    budget = _budget(amount=Decimal("900"), currency="EUR", hard_cap=True)
    book = InMemoryFxRateBook(
        [FxRate(base_currency="USD", quote_currency="EUR", rate=Decimal("0.9"), effective_from=NOW)]
    )
    status = evaluate_budget(budget, Decimal("1000"), fx=book, at=NOW)
    assert status.spend_in_budget_currency == Decimal("900.0")
    assert status.over_budget is False  # exactly at the cap, not over it


def test_missing_fx_rate_still_alerts_a_hard_cap_budget_never_silently_blind() -> None:
    budget = _budget(amount=Decimal("900"), currency="EUR", hard_cap=True)
    status = evaluate_budget(budget, Decimal("1000"), fx=InMemoryFxRateBook(), at=NOW)
    assert status.fx_unavailable is True
    assert status.alert is True
    assert status.spend_in_budget_currency is None
