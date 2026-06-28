"""Unit tests for the pure burndown math (F26 AC #9, #10)."""

from __future__ import annotations

from datetime import date, datetime

from forge_board.velocity import (
    ScopeEvent,
    SprintWindow,
    compute_burndown,
    ideal_line,
)
from forge_contracts.enums import SprintScopeEventType as ET

START = date(2026, 6, 1)
END = date(2026, 6, 5)
WINDOW = SprintWindow(start_date=START, end_date=END)


def test_ideal_line_reaches_zero_on_end_and_is_monotone() -> None:
    line = ideal_line(20, START, END)
    assert line[START] == 20.0
    assert line[END] == 0.0
    values = [line[d] for d in sorted(line)]
    assert values == sorted(values, reverse=True)  # monotone non-increasing


def test_ideal_line_single_day() -> None:
    line = ideal_line(20, START, START)
    assert line == {START: 0.0}


def _ev(day: int, et: ET, scope: int, remaining: int) -> ScopeEvent:
    return ScopeEvent(
        occurred_at=datetime(2026, 6, day, 12, 0),
        event_type=et,
        scope_points_after=scope,
        remaining_points_after=remaining,
    )


def test_compute_burndown_per_day_remaining() -> None:
    events = [
        _ev(1, ET.SPRINT_STARTED, 20, 20),
        _ev(2, ET.TASK_COMPLETED, 20, 15),  # -5 completed
        _ev(3, ET.TASK_ADDED, 25, 20),  # +5 scope
        _ev(4, ET.TASK_COMPLETED, 25, 12),  # -8 completed
    ]
    points = compute_burndown(WINDOW, 20, events, committed_task_count=4)
    assert [p.snapshot_date for p in points] == [
        date(2026, 6, d) for d in range(1, 6)
    ]
    remaining = [p.remaining_points for p in points]
    assert remaining == [20, 15, 20, 12, 12]  # day-5 carries day-4 state
    # completed_points = scope - remaining
    assert points[3].completed_points == 25 - 12
    # day-of-completion identity: committed(20) + added(5) - removed(0) - completed(13)
    assert points[-1].remaining_points == 20 + 5 - 0 - 13


def test_compute_burndown_respects_as_of() -> None:
    events = [_ev(1, ET.SPRINT_STARTED, 20, 20), _ev(2, ET.TASK_COMPLETED, 20, 15)]
    points = compute_burndown(WINDOW, 20, events, as_of=date(2026, 6, 2))
    assert [p.snapshot_date for p in points] == [date(2026, 6, 1), date(2026, 6, 2)]
    assert points[-1].remaining_points == 15


def test_compute_burndown_is_pure() -> None:
    events = [_ev(1, ET.SPRINT_STARTED, 20, 20), _ev(2, ET.TASK_COMPLETED, 20, 15)]
    assert compute_burndown(WINDOW, 20, events) == compute_burndown(WINDOW, 20, events)


def test_burndown_task_counts_reconstructed() -> None:
    events = [
        _ev(1, ET.SPRINT_STARTED, 20, 20),
        _ev(2, ET.TASK_ADDED, 25, 25),
        _ev(3, ET.TASK_COMPLETED, 25, 22),
    ]
    points = compute_burndown(WINDOW, 20, events, committed_task_count=4)
    # day 3: 4 committed + 1 added = 5 scope tasks; 1 completed; 4 remaining
    assert points[2].completed_task_count == 1
    assert points[2].remaining_task_count == 4
