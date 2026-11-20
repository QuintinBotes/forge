"""Risk at the Self-Eval Gate (#194 criteria 3 and 6).

Every case of ``test_self_eval_gate`` is collected here again and re-run with a
shadow reader that classifies every change as requiring a Self-Eval: each must
pass unchanged, with one observation per check. Enforcing mode makes a required
Self-Eval run and pass: a cold start, a missing suite or a forced override no
longer passes, and a regression still fails.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
import test_self_eval_gate as _matrix

from scf_contracts import GatedChange
from scf_contracts.testing import RecordingRiskReader
from scf_eval.sweval import (
    SelfEvalGate,
    SelfEvalRegressionError,
    SelfEvalRequiredError,
    SelfEvalScorecard,
)

# Criterion 3: the existing gate matrix, collected again under the shadow reader below.
globals().update({name: test for name, test in vars(_matrix).items() if name.startswith("test_")})

WS = uuid.uuid4()
CHANGE = GatedChange(workspace_id=WS, workflow_run_id=uuid.uuid4())


@pytest.fixture(autouse=True)
def _shadow(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    if request.function.__module__ != _matrix.__name__:
        yield
        return
    reader = RecordingRiskReader("shadow")
    check = SelfEvalGate.check_config

    async def with_risk(
        self: SelfEvalGate, workspace_id: UUID, config: Any, **kwargs: Any
    ) -> SelfEvalScorecard | None:
        classified = dataclasses.replace(self, risk=reader)
        return await check(classified, workspace_id, config, change=CHANGE, **kwargs)

    monkeypatch.setattr(SelfEvalGate, "check_config", with_risk)
    yield
    assert reader.looked_up, "every case checks the gate"
    reader.assert_observed_each_lookup("self_eval", {"pass", "block"})


def _gate(
    reader: RecordingRiskReader, *, rate: float | None, baseline: float | None
) -> SelfEvalGate:
    async def runner(_ws: UUID, _cfg: object) -> SelfEvalScorecard | None:
        if rate is None:
            return None
        return SelfEvalScorecard(total=10, resolved=int(rate * 10), resolution_rate=rate)

    return SelfEvalGate(eval_runner=runner, baseline_for=lambda _ws: baseline, risk=reader)


#: Ways the gate passes today without running a Self-Eval.
UNRUN = {
    "cold start": ({"rate": 0.9, "baseline": None}, False),
    "no private suite": ({"rate": None, "baseline": 0.9}, False),
    "forced": ({"rate": 0.1, "baseline": 0.9}, True),
}


@pytest.mark.parametrize("case", list(UNRUN))
async def test_shadow_passes_an_unrun_self_eval_and_records_that_risk_would_block(
    case: str,
) -> None:
    rates, force = UNRUN[case]
    reader = RecordingRiskReader("shadow")

    assert await _gate(reader, **rates).check_config(WS, {}, force=force, change=CHANGE) is None
    assert reader.observed == [{"gate": "self_eval", "actual": "pass", "risk_decision": "block"}]


@pytest.mark.parametrize("case", list(UNRUN))
async def test_enforcing_requires_the_self_eval_the_class_demands(case: str) -> None:
    rates, force = UNRUN[case]
    gate = _gate(RecordingRiskReader("enforcing"), **rates)

    with pytest.raises(SelfEvalRequiredError):
        await gate.check_config(WS, {}, force=force, change=CHANGE)


async def test_enforcing_passes_a_self_eval_that_ran_and_held_the_baseline() -> None:
    reader = RecordingRiskReader("enforcing")
    card = await _gate(reader, rate=0.9, baseline=0.9).check_config(WS, {}, change=CHANGE)
    assert card is not None and card.resolution_rate == 0.9
    assert reader.observed == [{"gate": "self_eval", "actual": "pass", "risk_decision": "pass"}]


async def test_enforcing_keeps_blocking_a_regression() -> None:
    reader = RecordingRiskReader("enforcing")
    with pytest.raises(SelfEvalRegressionError):
        await _gate(reader, rate=0.6, baseline=0.9).check_config(WS, {}, change=CHANGE)
    assert reader.observed == [{"gate": "self_eval", "actual": "block", "risk_decision": "block"}]


async def test_a_class_that_does_not_require_self_eval_changes_nothing() -> None:
    gate = _gate(RecordingRiskReader("enforcing", self_eval=False), rate=0.9, baseline=None)
    assert await gate.check_config(WS, {}, change=CHANGE) is None
