"""Unit tests for the pure velocity math (F26 AC #6, #11)."""

from __future__ import annotations

from datetime import date, datetime

from forge_board.velocity import (
    SprintTaskSnapshot,
    SprintWindow,
    VelocityResult,
    compute_velocity,
    compute_velocity_summary,
)

WINDOW = SprintWindow(start_date=date(2026, 6, 1), end_date=date(2026, 6, 14))
_T = datetime(2026, 6, 2)


def _committed(task_id: str, points: int, *, completed: bool = False) -> SprintTaskSnapshot:
    return SprintTaskSnapshot(
        task_id=task_id, points=points, is_completed=completed, in_committed_scope=True,
        completed_at=_T if completed else None,
    )


def test_all_completed_predictability_is_one() -> None:
    tasks = [_committed("a", 3, completed=True), _committed("b", 5, completed=True)]
    result = compute_velocity(WINDOW, tasks)
    assert result.committed_points == 8
    assert result.completed_points == 8
    assert result.predictability == 1.0
    assert result.carryover_points == 0


def test_zero_committed_no_divide_by_zero() -> None:
    result = compute_velocity(WINDOW, [])
    assert result.committed_points == 0
    assert result.predictability == 0.0
    assert result.scope_change_ratio == 0.0


def test_committed_completed_carryover_arithmetic() -> None:
    # committed 34, completed 25, carryover 9 (AC #7 numbers).
    tasks = [
        _committed("a", 10, completed=True),
        _committed("b", 10, completed=True),
        _committed("c", 5, completed=True),
        _committed("d", 4),  # carryover
        _committed("e", 5),  # carryover
    ]
    result = compute_velocity(WINDOW, tasks)
    assert result.committed_points == 34
    assert result.completed_points == 25
    assert result.carryover_points == 9
    assert result.carryover_task_count == 2
    assert result.predictability == round(25 / 34, 4)


def test_added_and_removed_scope() -> None:
    tasks = [
        _committed("a", 10, completed=True),
        SprintTaskSnapshot(task_id="add", points=5, added_at=_T),  # still in sprint
        SprintTaskSnapshot(
            task_id="add_done", points=3, added_at=_T, is_completed=True, completed_at=_T
        ),
        SprintTaskSnapshot(task_id="rm", points=4, in_committed_scope=True, removed_at=_T),
    ]
    result = compute_velocity(WINDOW, tasks)
    # committed includes the removed committed task (snapshot at start).
    assert result.committed_points == 14
    assert result.added_points == 8  # 5 + 3
    assert result.removed_points == 4
    # completed = committed-done (10) + added-done (3)
    assert result.completed_points == 13
    # carryover = the still-in-sprint added not-done task (5)
    assert result.carryover_points == 5
    assert result.scope_change_ratio == round((8 + 4) / 14, 4)


def test_determinism() -> None:
    tasks = [_committed("a", 3, completed=True), _committed("b", 5)]
    assert compute_velocity(WINDOW, tasks) == compute_velocity(WINDOW, tasks)


def _vr(committed: int, completed: int) -> VelocityResult:
    pred = round(completed / committed, 4) if committed else 0.0
    return VelocityResult(
        committed_points=committed, completed_points=completed, predictability=pred
    )


def test_summary_average_rolling3_forecast() -> None:
    history = [_vr(20, 20), _vr(30, 24), _vr(30, 30), _vr(20, 18), _vr(40, 28)]
    summary = compute_velocity_summary(history)
    completed = [20, 24, 30, 18, 28]
    assert summary.sprint_count == 5
    assert summary.average_velocity == round(sum(completed) / 5, 2)
    assert summary.rolling_3_velocity == round((30 + 18 + 28) / 3, 2)
    assert summary.forecast_low == 18.0
    assert summary.forecast_high == 30.0


def test_summary_empty_is_zeros() -> None:
    summary = compute_velocity_summary([])
    assert summary.sprint_count == 0
    assert summary.average_velocity == 0.0
    assert summary.rolling_3_velocity == 0.0
    assert summary.forecast_high == 0.0
