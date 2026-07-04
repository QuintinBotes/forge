"""Freeze-window logic — wrap-around windows, timezones, next_open."""

from __future__ import annotations

from datetime import UTC, datetime, time

import pytest

from forge_deploy.freeze import is_frozen, next_open
from forge_deploy.schemas import FreezeWindow

# Fri 17:00 -> Mon 09:00 weekend freeze (wrap-around the week boundary).
WEEKEND = FreezeWindow(
    start_day=4, start_time=time(17, 0), end_day=0, end_time=time(9, 0), reason="weekend"
)


@pytest.mark.parametrize(
    ("instant", "frozen"),
    [
        (datetime(2026, 6, 26, 16, 59, tzinfo=UTC), False),  # Fri 16:59 - open
        (datetime(2026, 6, 26, 17, 0, tzinfo=UTC), True),    # Fri 17:00 - frozen
        (datetime(2026, 6, 27, 12, 0, tzinfo=UTC), True),    # Sat noon - frozen
        (datetime(2026, 6, 28, 23, 59, tzinfo=UTC), True),   # Sun late - frozen
        (datetime(2026, 6, 29, 8, 59, tzinfo=UTC), True),    # Mon 08:59 - frozen
        (datetime(2026, 6, 29, 9, 0, tzinfo=UTC), False),    # Mon 09:00 - open
        (datetime(2026, 6, 24, 12, 0, tzinfo=UTC), False),   # Wed noon - open
    ],
)
def test_weekend_wraparound(instant: datetime, frozen: bool) -> None:
    state = is_frozen([WEEKEND], instant)
    assert state.frozen is frozen


def test_no_windows_never_frozen() -> None:
    assert is_frozen([], datetime(2026, 6, 27, 12, 0, tzinfo=UTC)).frozen is False


def test_until_is_window_end() -> None:
    state = is_frozen([WEEKEND], datetime(2026, 6, 27, 12, 0, tzinfo=UTC))
    assert state.frozen is True
    assert state.until == datetime(2026, 6, 29, 9, 0, tzinfo=UTC)


def test_next_open_none_when_open() -> None:
    assert next_open([WEEKEND], datetime(2026, 6, 24, 12, 0, tzinfo=UTC)) is None


def test_timezone_is_respected() -> None:
    # A non-wrap window 09:00-17:00 Mon in New York; 13:00 UTC Mon == 09:00 EDT.
    window = FreezeWindow(
        start_day=0, start_time=time(9, 0), end_day=0, end_time=time(17, 0)
    )
    instant = datetime(2026, 6, 29, 18, 0, tzinfo=UTC)  # 14:00 EDT Mon / 18:00 UTC
    assert is_frozen([window], instant, "America/New_York").frozen is True
    assert is_frozen([window], instant, "UTC").frozen is False
