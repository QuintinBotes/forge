"""Shadow means shadow (spec A §5.4, §8; #194 criteria 3 and 4).

``gate_decision`` is how every shipping gate consults risk: in shadow mode it
returns the gate's own decision and records what risk would have decided; when
enforcing it returns the tightened one; a reader that fails changes nothing.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import pytest

from scf_contracts import (
    ClassifiedChange,
    GatedChange,
    GateRequirements,
    RiskClass,
    RiskEnforcement,
    RiskReader,
    gate_decision,
    tighten_approvals,
    tighten_check,
)

WS = uuid.uuid4()
CHANGE = GatedChange(workspace_id=WS, workflow_run_id=uuid.uuid4())
STRICT = GateRequirements(
    risk_class=RiskClass.CRITICAL, approvals=3, deploy_mode="gated", red_team=True, self_eval=True
)


class _Reader:
    def __init__(
        self, mode: RiskEnforcement = "shadow", *, found: bool = True, fail: str | None = None
    ) -> None:
        self.mode, self.found, self.fail = mode, found, fail
        self.observed: list[dict[str, Any]] = []

    def risk_for(self, change: GatedChange) -> ClassifiedChange | None:
        if self.fail == "read":
            raise RuntimeError("classification unreadable")
        if not self.found:
            return None
        return ClassifiedChange(
            classification_id=uuid.uuid4(),
            workspace_id=change.workspace_id,
            enforcement=self.mode,
            requirements=STRICT,
        )

    def observe(
        self, risk: ClassifiedChange, *, gate: str, actual: str, risk_decision: str
    ) -> None:
        if self.fail == "observe":
            raise RuntimeError("observation not stored")
        self.observed.append({"gate": gate, "actual": actual, "risk_decision": risk_decision})


def _approvals(own: int, req: GateRequirements) -> int:
    return tighten_approvals(own, req.approvals)


def test_a_reader_satisfies_the_protocol() -> None:
    assert isinstance(_Reader(), RiskReader)


def test_without_a_reader_or_a_change_the_gate_decides_alone() -> None:
    reader = _Reader()
    assert gate_decision(None, CHANGE, "approval", 1, _approvals) == 1
    assert gate_decision(reader, None, "approval", 1, _approvals) == 1
    assert reader.observed == []


def test_an_unclassified_change_is_not_observed() -> None:
    reader = _Reader(found=False)
    assert gate_decision(reader, CHANGE, "approval", 1, _approvals) == 1
    assert reader.observed == []


def test_shadow_returns_the_gates_own_decision_and_records_what_risk_would_have() -> None:
    reader = _Reader("shadow")
    assert gate_decision(reader, CHANGE, "approval", 1, _approvals) == 1
    assert reader.observed == [{"gate": "approval", "actual": "1", "risk_decision": "3"}]


def test_enforcing_returns_the_tightened_decision_and_records_both() -> None:
    reader = _Reader("enforcing")
    assert gate_decision(reader, CHANGE, "approval", 1, _approvals) == 3
    assert reader.observed == [{"gate": "approval", "actual": "1", "risk_decision": "3"}]


def test_a_check_decision_is_recorded_as_pass_or_block() -> None:
    reader = _Reader("shadow")

    def red_team(passes: bool, req: GateRequirements) -> bool:
        return tighten_check(passes, required=req.red_team, satisfied=False)

    assert gate_decision(reader, CHANGE, "red_team", True, red_team) is True
    assert reader.observed == [{"gate": "red_team", "actual": "pass", "risk_decision": "block"}]


@pytest.mark.parametrize("mode", ["shadow", "enforcing"])
def test_a_reader_that_cannot_read_changes_nothing_and_is_logged(
    mode: RiskEnforcement, caplog: pytest.LogCaptureFixture
) -> None:
    reader = _Reader(mode, fail="read")
    with caplog.at_level(logging.ERROR):
        assert gate_decision(reader, CHANGE, "approval", 1, _approvals) == 1
    assert reader.observed == []
    assert "approval" in caplog.text and "classification unreadable" in caplog.text


@pytest.mark.parametrize(("mode", "decided"), [("shadow", 1), ("enforcing", 3)])
def test_an_observation_that_fails_leaves_the_decision_to_the_mode(
    mode: RiskEnforcement, decided: int, caplog: pytest.LogCaptureFixture
) -> None:
    reader = _Reader(mode, fail="observe")
    with caplog.at_level(logging.ERROR):
        assert gate_decision(reader, CHANGE, "approval", 1, _approvals) == decided
    assert "observation not stored" in caplog.text
