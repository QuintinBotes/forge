"""Unit tests for the F26 sprint state machine (AC: lifecycle transitions)."""

from __future__ import annotations

import pytest

from forge_board.exceptions import SprintStateError
from forge_board.sprint_state import SprintStateMachine
from forge_contracts.enums import SprintState


@pytest.fixture
def sm() -> SprintStateMachine:
    return SprintStateMachine()


@pytest.mark.parametrize(
    "frm,to",
    [
        (SprintState.PLANNED, SprintState.ACTIVE),
        (SprintState.PLANNED, SprintState.CANCELLED),
        (SprintState.ACTIVE, SprintState.COMPLETED),
        (SprintState.ACTIVE, SprintState.CANCELLED),
    ],
)
def test_allowed_transitions(sm: SprintStateMachine, frm, to) -> None:
    assert sm.can_transition(frm, to)
    sm.assert_transition(frm, to)  # does not raise


@pytest.mark.parametrize(
    "frm,to",
    [
        (SprintState.COMPLETED, SprintState.ACTIVE),
        (SprintState.ACTIVE, SprintState.PLANNED),
        (SprintState.CANCELLED, SprintState.ACTIVE),
        (SprintState.PLANNED, SprintState.COMPLETED),
        (SprintState.COMPLETED, SprintState.CANCELLED),
    ],
)
def test_rejected_transitions(sm: SprintStateMachine, frm, to) -> None:
    assert not sm.can_transition(frm, to)
    with pytest.raises(SprintStateError):
        sm.assert_transition(frm, to)
