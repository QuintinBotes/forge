"""Risk only tightens a shipping gate (spec A §4.1, #194 criterion 5).

Each decision type has one tighten function; every gate composes its own decision
with a class's requirements through one of them. The tests enumerate every
combination, so a gate can never come out more permissive than it went in.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Callable
from typing import Any

import pytest

from scf_contracts import (
    GatedChange,
    GateRequirements,
    gate_decision,
    tighten_approvals,
    tighten_check,
    tighten_deploy,
)
from scf_contracts.testing import RecordingRiskReader

WS = uuid.uuid4()
COUNTS = range(5)
MODES = ("auto", "gated")
BOOLS = (False, True)


@pytest.mark.parametrize(("existing", "required"), list(itertools.product(COUNTS, COUNTS)))
def test_approvals_take_the_higher_count(existing: int, required: int) -> None:
    tightened = tighten_approvals(existing, required)
    assert tightened >= existing and tightened >= required
    assert tightened in (existing, required)


@pytest.mark.parametrize(
    ("existing", "required", "expected"),
    [
        ("auto", "auto", "auto"),
        ("auto", "gated", "gated"),
        ("gated", "auto", "gated"),
        ("gated", "gated", "gated"),
    ],
)
def test_a_deploy_is_gated_when_either_side_gates_it(
    existing: str, required: str, expected: str
) -> None:
    assert tighten_deploy(existing, required) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("passes", "required", "satisfied"), list(itertools.product(BOOLS, repeat=3))
)
def test_a_required_check_passes_only_when_satisfied(
    passes: bool, required: bool, satisfied: bool
) -> None:
    tightened = tighten_check(passes, required=required, satisfied=satisfied)
    assert tightened == (passes and (satisfied or not required))
    assert not (tightened and not passes)  # never opens a failed check


#: Every requirement a class can carry.
REQUIREMENTS = [
    {"approvals": a, "deploy_mode": m, "red_team": r, "self_eval": e}
    for a, m, r, e in itertools.product(COUNTS, MODES, BOOLS, BOOLS)
]

Tighten = Callable[[Any, GateRequirements], Any]


def _check(field: str, satisfied: bool) -> Tighten:
    def tighten(passes: bool, req: GateRequirements) -> bool:
        required = req.approvals > 0 if field == "approvals" else bool(getattr(req, field))
        return tighten_check(passes, required=required, satisfied=satisfied)

    return tighten


#: Each gate's own decisions, how it tightens them and how strict each is (higher = stricter).
GATES: dict[str, tuple[list[Any], Callable[[Any], Tighten], Callable[[Any], int]]] = {
    "approval": (
        list(COUNTS),
        lambda _: lambda own, req: tighten_approvals(own, req.approvals),
        int,
    ),
    "deploy": (
        list(MODES),
        lambda _: lambda own, req: tighten_deploy(own, req.deploy_mode),
        MODES.index,
    ),
    # A check gate's own decision is (passes, whether its control was satisfied).
    "red_team": (
        list(itertools.product(BOOLS, BOOLS)),
        lambda state: _check("red_team", state[1]),
        lambda passes: int(not passes),
    ),
    "self_eval": (
        list(itertools.product(BOOLS, BOOLS)),
        lambda state: _check("self_eval", state[1]),
        lambda passes: int(not passes),
    ),
    "merge": (
        list(itertools.product(BOOLS, BOOLS)),
        lambda state: _check("approvals", state[1]),
        lambda passes: int(not passes),
    ),
}


@pytest.mark.parametrize("gate", list(GATES))
def test_enforcing_is_never_more_permissive_than_shadow_at_any_gate(gate: str) -> None:
    decisions, tighten_for, strictness = GATES[gate]
    change = GatedChange(workspace_id=WS, commit_sha="abc")
    combinations = 0
    for state, req in itertools.product(decisions, REQUIREMENTS):
        own = state[0] if isinstance(state, tuple) else state
        outcomes = {
            mode: gate_decision(
                RecordingRiskReader(mode, **req),  # type: ignore[arg-type]
                change,
                gate,  # type: ignore[arg-type]
                own,
                tighten_for(state),
            )
            for mode in ("shadow", "enforcing")
        }
        assert outcomes["shadow"] == own
        assert strictness(outcomes["enforcing"]) >= strictness(outcomes["shadow"]), (state, req)
        combinations += 1
    assert combinations == len(decisions) * len(REQUIREMENTS)
