"""Tests for the Self-Eval Gate blocking logic (pure, no sandbox)."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from forge_eval.sweval import SelfEvalGate, SelfEvalRegressionError, SelfEvalScorecard

WS = uuid4()


def _card(rate: float) -> SelfEvalScorecard:
    return SelfEvalScorecard(total=10, resolved=int(rate * 10), resolution_rate=rate)


def _gate(*, rate: float | None, baseline: float | None) -> SelfEvalGate:
    async def runner(_ws: UUID, _cfg: object) -> SelfEvalScorecard | None:
        return _card(rate) if rate is not None else None

    return SelfEvalGate(eval_runner=runner, baseline_for=lambda _ws: baseline)


@pytest.mark.asyncio
async def test_regression_is_blocked() -> None:
    gate = _gate(rate=0.6, baseline=0.9)
    with pytest.raises(SelfEvalRegressionError):
        await gate.check_config(WS, {"model": "cheap"})


@pytest.mark.asyncio
async def test_equal_or_better_is_allowed() -> None:
    assert (await _gate(rate=0.9, baseline=0.9).check_config(WS, {})).resolution_rate == 0.9
    assert (await _gate(rate=0.95, baseline=0.9).check_config(WS, {})).resolution_rate == 0.95


@pytest.mark.asyncio
async def test_cold_start_no_baseline_is_noop() -> None:
    assert await _gate(rate=0.1, baseline=None).check_config(WS, {}) is None


@pytest.mark.asyncio
async def test_no_private_suite_is_noop() -> None:
    assert await _gate(rate=None, baseline=0.9).check_config(WS, {}) is None


@pytest.mark.asyncio
async def test_force_overrides_the_gate() -> None:
    # A regressing config passes when forced (the caller audits the override).
    gate = _gate(rate=0.1, baseline=0.9)
    assert await gate.check_config(WS, {}, force=True) is None
