"""Tests for the task status workflow rules (plan Task 1.5: status workflow rules)."""

from __future__ import annotations

import pytest

from forge_board.exceptions import InvalidStatusTransitionError
from forge_board.workflow import (
    TASK_STATUS_TRANSITIONS,
    TERMINAL_STATUSES,
    allowed_transitions,
    can_transition,
    validate_transition,
)
from forge_contracts import TaskStatus


def test_every_status_has_a_transition_entry() -> None:
    # The workflow table must be total over the status enum.
    for status in TaskStatus:
        assert status in TASK_STATUS_TRANSITIONS


def test_forward_path_is_allowed() -> None:
    assert can_transition(TaskStatus.BACKLOG, TaskStatus.READY)
    assert can_transition(TaskStatus.READY, TaskStatus.IN_PROGRESS)
    assert can_transition(TaskStatus.IN_PROGRESS, TaskStatus.IN_REVIEW)
    assert can_transition(TaskStatus.IN_REVIEW, TaskStatus.DONE)


def test_illegal_jump_is_rejected() -> None:
    # You cannot jump straight from backlog to done.
    assert not can_transition(TaskStatus.BACKLOG, TaskStatus.DONE)
    assert not can_transition(TaskStatus.BACKLOG, TaskStatus.IN_REVIEW)


def test_same_status_is_idempotent_noop() -> None:
    assert can_transition(TaskStatus.IN_PROGRESS, TaskStatus.IN_PROGRESS)
    # validate_transition must not raise for a no-op.
    validate_transition(TaskStatus.IN_PROGRESS, TaskStatus.IN_PROGRESS)


def test_validate_transition_raises_on_illegal() -> None:
    with pytest.raises(InvalidStatusTransitionError):
        validate_transition(TaskStatus.BACKLOG, TaskStatus.DONE)


def test_terminal_states_can_be_reopened_but_not_advance() -> None:
    assert TaskStatus.DONE in TERMINAL_STATUSES
    assert TaskStatus.CANCELLED in TERMINAL_STATUSES
    # done can be reopened to in_progress, but not pushed to in_review directly.
    assert can_transition(TaskStatus.DONE, TaskStatus.IN_PROGRESS)
    assert not can_transition(TaskStatus.DONE, TaskStatus.IN_REVIEW)


def test_allowed_transitions_returns_targets() -> None:
    targets = allowed_transitions(TaskStatus.IN_PROGRESS)
    assert TaskStatus.IN_REVIEW in targets
    assert TaskStatus.DONE in targets
