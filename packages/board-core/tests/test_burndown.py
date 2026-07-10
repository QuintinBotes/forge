"""Unit tests for the pure burndown math (F26 AC #9, #10)."""

from __future__ import annotations

from datetime import date, datetime

from forge_board.velocity import (
    ScopeEvent,
    SprintWindow,
    WorkCalendar,
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
    assert [p.snapshot_date for p in points] == [date(2026, 6, d) for d in range(1, 6)]
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


# --------------------------------------------------------------------------- #
# F40 PM depth: working-day/holiday calendar                                   #
# --------------------------------------------------------------------------- #


def test_ideal_line_no_calendar_matches_empty_calendar() -> None:
    """An empty/default calendar is byte-identical to the calendar-free line."""
    assert ideal_line(20, START, END) == ideal_line(20, START, END, WorkCalendar())


def test_ideal_line_skips_weekend_days() -> None:
    # 2026-06-01 is a Monday; window runs Mon..Sat (6 days), Sat is day 5 (index).
    end = date(2026, 6, 6)  # Saturday
    cal = WorkCalendar(weekend_days=frozenset({5, 6}))  # Sat, Sun
    line = ideal_line(20, START, end, cal)
    # Working days: Mon..Fri (5 days) -> steps of 20/4=5; Saturday holds flat.
    assert line[date(2026, 6, 1)] == 20.0
    assert line[date(2026, 6, 2)] == 15.0
    assert line[date(2026, 6, 5)] == 0.0
    assert line[date(2026, 6, 6)] == 0.0  # Saturday: flat at Friday's value


def test_ideal_line_skips_a_holiday() -> None:
    cal = WorkCalendar(holidays=frozenset({date(2026, 6, 3)}))
    line = ideal_line(20, START, END, cal)
    # 4 working days remain (1,2,4,5); day 3 (holiday) holds flat at day 2's value.
    assert line[date(2026, 6, 2)] == line[date(2026, 6, 3)]
    assert line[END] == 0.0


def test_ideal_line_all_non_working_holds_flat_at_committed() -> None:
    """No working day in the window -> nothing has started burning -> flat at
    the committed value for every calendar day (not zeroed)."""
    cal = WorkCalendar(weekend_days=frozenset(range(7)))  # every day is a weekend
    line = ideal_line(20, START, END, cal)
    assert line == {date(2026, 6, d): 20.0 for d in range(1, 6)}


def test_ideal_line_lone_working_day_holds_prior_days_at_committed() -> None:
    """Regression: a window that is mostly holidays with a single working day
    at the end must NOT zero the whole window — days before the lone working
    day haven't started burning yet and should read the committed value."""
    end = date(2026, 6, 5)
    cal = WorkCalendar(holidays=frozenset(date(2026, 6, d) for d in range(1, 5)))
    line = ideal_line(10, START, end, cal)
    assert line[date(2026, 6, 1)] == 10.0
    assert line[date(2026, 6, 2)] == 10.0
    assert line[date(2026, 6, 3)] == 10.0
    assert line[date(2026, 6, 4)] == 10.0
    assert line[date(2026, 6, 5)] == 0.0


def test_compute_burndown_with_calendar_reaches_ideal_zero_on_last_working_day() -> None:
    cal = WorkCalendar(weekend_days=frozenset({5, 6}))
    events = [_ev(1, ET.SPRINT_STARTED, 20, 20)]
    end = date(2026, 6, 6)
    window = SprintWindow(start_date=START, end_date=end)
    points = compute_burndown(window, 20, events, calendar=cal)
    by_day = {p.snapshot_date: p for p in points}
    assert by_day[date(2026, 6, 5)].ideal_points == 0.0
    assert by_day[date(2026, 6, 6)].ideal_points == 0.0
