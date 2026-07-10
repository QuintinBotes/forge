"""Pure sprint velocity / burndown math (F26).

No I/O, deterministic: identical inputs -> identical output. The lifecycle
service and the reconcile/snapshot worker tasks assemble inputs from the DB and
call these functions, so the rollup + burndown are reconstructable from the
append-only ``sprint_scope_event`` log + current task rows.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from pydantic import BaseModel

from forge_contracts.enums import SprintScopeEventType

# Ratios are stored in Numeric(5,4); round to 4 dp so the live path and the
# reconcile path produce byte-identical persisted values.
_RATIO_DP = 4


class WorkCalendar(BaseModel):
    """Working-day calendar the burndown ideal-line reads (F40 PM depth).

    ``weekend_days`` is a denylist of ISO weekdays (Monday=0..Sunday=6) treated
    as non-working; ``holidays`` is a set of ad-hoc non-working dates. Both
    default empty, which makes every day working — byte-identical to the
    pre-F40 calendar-free ideal line.
    """

    weekend_days: frozenset[int] = frozenset()
    holidays: frozenset[date] = frozenset()

    def is_working_day(self, day: date) -> bool:
        """True iff ``day`` is neither a configured weekend weekday nor a holiday."""
        return day.weekday() not in self.weekend_days and day not in self.holidays


class SprintWindow(BaseModel):
    """The sprint's calendar window + lifecycle timestamps."""

    start_date: date
    end_date: date
    started_at: datetime | None = None
    completed_at: datetime | None = None


class SprintTaskSnapshot(BaseModel):
    """A task's contribution to a sprint at compute time."""

    task_id: str
    points: int = 0  # tasks.estimate or 0 if None
    is_completed: bool = False  # current status == DONE
    is_cancelled: bool = False  # current status == CANCELLED
    in_committed_scope: bool = False  # in sprint, non-done, non-cancelled at start
    added_at: datetime | None = None  # set if added to the sprint after started_at
    removed_at: datetime | None = None  # set if removed from the sprint after started_at
    completed_at: datetime | None = None  # when it crossed into the completed status


class VelocityResult(BaseModel):
    """The per-sprint velocity rollup."""

    committed_points: int = 0
    completed_points: int = 0
    added_points: int = 0
    removed_points: int = 0
    carryover_points: int = 0
    committed_task_count: int = 0
    completed_task_count: int = 0
    carryover_task_count: int = 0
    predictability: float = 0.0  # completed/committed, 0.0 if committed == 0
    scope_change_ratio: float = 0.0  # (added+removed)/committed, 0.0 if committed == 0


class ScopeEvent(BaseModel):
    """A mirror of a ``sprint_scope_event`` row, ordered by ``occurred_at``."""

    occurred_at: datetime
    event_type: SprintScopeEventType
    points_delta: int = 0
    scope_points_after: int = 0
    remaining_points_after: int = 0


class BurndownPoint(BaseModel):
    """One calendar day of a sprint's burndown."""

    snapshot_date: date
    scope_points: int = 0
    remaining_points: int = 0
    completed_points: int = 0
    ideal_points: float = 0.0
    completed_task_count: int = 0
    remaining_task_count: int = 0


class VelocitySummary(BaseModel):
    """Aggregate velocity over a window of completed sprints."""

    sprint_count: int = 0
    average_velocity: float = 0.0
    rolling_3_velocity: float = 0.0
    predictability_avg: float = 0.0
    scope_change_avg: float = 0.0
    forecast_low: float = 0.0
    forecast_avg: float = 0.0
    forecast_high: float = 0.0


def compute_velocity(window: SprintWindow, tasks: list[SprintTaskSnapshot]) -> VelocityResult:
    """Compute the per-sprint velocity rollup from task snapshots (pure)."""
    committed = [t for t in tasks if t.in_committed_scope]
    committed_points = sum(t.points for t in committed)
    committed_task_count = len(committed)

    in_sprint = [t for t in tasks if t.removed_at is None]
    completed = [
        t for t in in_sprint if t.is_completed and (t.in_committed_scope or t.added_at is not None)
    ]
    completed_points = sum(t.points for t in completed)
    completed_task_count = len(completed)

    added = [t for t in in_sprint if t.added_at is not None and not t.in_committed_scope]
    added_points = sum(t.points for t in added)

    removed = [t for t in tasks if t.removed_at is not None]
    removed_points = sum(t.points for t in removed)

    carry = [
        t
        for t in in_sprint
        if not t.is_completed
        and not t.is_cancelled
        and (t.in_committed_scope or t.added_at is not None)
    ]
    carryover_points = sum(t.points for t in carry)
    carryover_task_count = len(carry)

    predictability = (
        round(completed_points / committed_points, _RATIO_DP) if committed_points else 0.0
    )
    scope_change_ratio = (
        round((added_points + removed_points) / committed_points, _RATIO_DP)
        if committed_points
        else 0.0
    )

    return VelocityResult(
        committed_points=committed_points,
        completed_points=completed_points,
        added_points=added_points,
        removed_points=removed_points,
        carryover_points=carryover_points,
        committed_task_count=committed_task_count,
        completed_task_count=completed_task_count,
        carryover_task_count=carryover_task_count,
        predictability=predictability,
        scope_change_ratio=scope_change_ratio,
    )


def _days(start: date, end: date) -> list[date]:
    if end < start:
        return [start]
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def ideal_line(
    committed_points: int,
    start: date,
    end: date,
    calendar: WorkCalendar | None = None,
) -> dict[date, float]:
    """Linear committed->0 across inclusive *working* calendar days; end-day == 0.0.

    Non-working days (per ``calendar``, if given) hold flat at the prior
    working day's value — no burn is expected on a weekend/holiday, so days
    *before* the first working day still read the initial committed value
    (nothing has started burning yet). With no calendar (or one with no
    configured weekend days/holidays) every day is working, reproducing the
    plain linear line byte-for-byte. A window with a single working day burns
    straight to zero on that day (it is simultaneously the window's first and
    last working day). A window with *no* working days never starts burning,
    so it holds flat at the committed value for its entire length.
    """
    cal = calendar or WorkCalendar()
    days = _days(start, end)
    work_days = [d for d in days if cal.is_working_day(d)]
    n = len(work_days) - 1
    if not work_days:
        work_value: dict[date, float] = {}
    elif n == 0:
        work_value = {work_days[0]: 0.0}
    else:
        work_value = {
            wd: round(committed_points * (n - k) / n, 2) for k, wd in enumerate(work_days)
        }
    result: dict[date, float] = {}
    carry = float(committed_points)
    for d in days:
        if d in work_value:
            carry = work_value[d]
        result[d] = carry
    return result


def _reconstruct_counts(
    events: list[ScopeEvent], committed_task_count: int
) -> tuple[int, int, int]:
    """Reconstruct (scope, remaining, completed) task counts from event types."""
    scope = remaining = completed = 0
    for ev in events:
        t = ev.event_type
        if t == SprintScopeEventType.SPRINT_STARTED:
            scope = remaining = committed_task_count
            completed = 0
        elif t == SprintScopeEventType.TASK_ADDED:
            scope += 1
            remaining += 1
        elif t == SprintScopeEventType.TASK_REMOVED:
            scope -= 1
            remaining -= 1
        elif t == SprintScopeEventType.TASK_COMPLETED:
            completed += 1
            remaining -= 1
        elif t == SprintScopeEventType.TASK_REOPENED:
            completed -= 1
            remaining += 1
        # ESTIMATE_CHANGED / SPRINT_COMPLETED / SPRINT_CANCELLED: no count change
    return scope, remaining, completed


def compute_burndown(
    window: SprintWindow,
    committed_points: int,
    events: list[ScopeEvent],
    *,
    as_of: date | None = None,
    committed_task_count: int = 0,
    calendar: WorkCalendar | None = None,
) -> list[BurndownPoint]:
    """One :class:`BurndownPoint` per calendar day, end-of-day state from the
    last event on/before that day (pure)."""
    start = window.start_date
    end = window.end_date
    last_day = min(end, as_of) if as_of is not None else end
    if last_day < start:
        last_day = start

    ideal = ideal_line(committed_points, start, end, calendar)
    ordered = sorted(events, key=lambda e: e.occurred_at)

    points: list[BurndownPoint] = []
    for day in _days(start, last_day):
        applied = [e for e in ordered if e.occurred_at.date() <= day]
        if applied:
            scope_points = applied[-1].scope_points_after
            remaining_points = applied[-1].remaining_points_after
            _scope_tc, remaining_tc, completed_tc = _reconstruct_counts(
                applied, committed_task_count
            )
        else:
            scope_points = remaining_points = committed_points
            remaining_tc = committed_task_count
            completed_tc = 0
        points.append(
            BurndownPoint(
                snapshot_date=day,
                scope_points=scope_points,
                remaining_points=remaining_points,
                completed_points=scope_points - remaining_points,
                ideal_points=ideal.get(day, 0.0),
                completed_task_count=completed_tc,
                remaining_task_count=remaining_tc,
            )
        )
    return points


def compute_velocity_summary(history: list[VelocityResult]) -> VelocitySummary:
    """Aggregate over completed sprints (newest last); empty -> all zeros."""
    if not history:
        return VelocitySummary()
    completed = [h.completed_points for h in history]
    n = len(history)
    average = sum(completed) / n
    last3 = completed[-3:]
    rolling3 = sum(last3) / len(last3)
    return VelocitySummary(
        sprint_count=n,
        average_velocity=round(average, 2),
        rolling_3_velocity=round(rolling3, 2),
        predictability_avg=round(sum(h.predictability for h in history) / n, _RATIO_DP),
        scope_change_avg=round(sum(h.scope_change_ratio for h in history) / n, _RATIO_DP),
        forecast_low=float(min(completed)),
        forecast_avg=round(average, 2),
        forecast_high=float(max(completed)),
    )


__all__ = [
    "BurndownPoint",
    "ScopeEvent",
    "SprintTaskSnapshot",
    "SprintWindow",
    "VelocityResult",
    "VelocitySummary",
    "WorkCalendar",
    "compute_burndown",
    "compute_velocity",
    "compute_velocity_summary",
    "ideal_line",
]
