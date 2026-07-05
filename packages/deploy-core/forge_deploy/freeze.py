"""Freeze-window evaluation — pure, deterministic time logic.

A :class:`~forge_deploy.schemas.FreezeWindow` is a weekly recurring window in the
pipeline timezone; while ``now`` falls inside any window, deploys are blocked.
Windows may wrap across the week boundary (Fri 17:00 -> Mon 09:00). All logic is
pure over an injected :class:`Clock` so freeze behaviour is table-testable with a
:class:`FakeClock`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from forge_deploy.schemas import FreezeWindow

_MINUTES_PER_WEEK = 7 * 24 * 60


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    """The real wall clock (timezone-aware UTC)."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """A fixed, settable clock for tests."""

    def __init__(self, instant: datetime) -> None:
        self._instant = instant if instant.tzinfo else instant.replace(tzinfo=UTC)

    def now(self) -> datetime:
        return self._instant

    def set(self, instant: datetime) -> None:
        self._instant = instant if instant.tzinfo else instant.replace(tzinfo=UTC)


class FreezeState(BaseModel):
    frozen: bool
    reason: str | None = None
    until: datetime | None = None


def _minute_of_week(local: datetime) -> int:
    return local.weekday() * 1440 + local.hour * 60 + local.minute


def _window_minutes(window: FreezeWindow) -> tuple[int, int]:
    start = window.start_day * 1440 + window.start_time.hour * 60 + window.start_time.minute
    end = window.end_day * 1440 + window.end_time.hour * 60 + window.end_time.minute
    return start, end


def _contains(window: FreezeWindow, minute_of_week: int) -> bool:
    start, end = _window_minutes(window)
    if start == end:
        return False
    if start < end:
        return start <= minute_of_week < end
    # Wrap-around window (e.g. Fri 17:00 -> Mon 09:00).
    return minute_of_week >= start or minute_of_week < end


def _next_end(window: FreezeWindow, local: datetime) -> datetime:
    """The first window-end boundary strictly after ``local`` (tz-aware)."""
    days_ahead = (window.end_day - local.weekday()) % 7
    candidate = datetime.combine(
        (local + timedelta(days=days_ahead)).date(),
        window.end_time,
        tzinfo=local.tzinfo,
    )
    if candidate <= local:
        candidate += timedelta(days=7)
    return candidate


def is_frozen(windows: list[FreezeWindow], now: datetime, timezone: str = "UTC") -> FreezeState:
    """Return whether ``now`` falls inside any freeze window."""
    if not windows:
        return FreezeState(frozen=False)
    tz = ZoneInfo(timezone)
    local = (now if now.tzinfo else now.replace(tzinfo=UTC)).astimezone(tz)
    mow = _minute_of_week(local)
    for window in windows:
        if _contains(window, mow):
            return FreezeState(
                frozen=True,
                reason=window.reason,
                until=_next_end(window, local).astimezone(UTC),
            )
    return FreezeState(frozen=False)


def next_open(windows: list[FreezeWindow], now: datetime, timezone: str = "UTC") -> datetime | None:
    """When the environment next leaves all freeze windows, or ``None`` if open now."""
    state = is_frozen(windows, now, timezone)
    return state.until if state.frozen else None


__all__ = [
    "Clock",
    "FakeClock",
    "FreezeState",
    "SystemClock",
    "is_frozen",
    "next_open",
]
