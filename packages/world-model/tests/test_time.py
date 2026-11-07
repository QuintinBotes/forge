"""Pure tests for forge_world.time (spec §3.1: two clocks, precisions)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from conftest import T0, make_fact

from forge_world import Precision, held_at, holds_at, parse_bound
from forge_world.time import bucket_end, bucket_start


def _dt(
    year: int, month: int = 1, day: int = 1, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "instant", "precision"),
    [
        ("2024", _dt(2024), Precision.YEAR),
        ("2024-03", _dt(2024, 3), Precision.MONTH),
        ("2024-03-15", _dt(2024, 3, 15), Precision.DAY),
        ("2024-03-15T10:00:00Z", _dt(2024, 3, 15, 10), Precision.SECOND),
        ("2024-03-15T10:30Z", _dt(2024, 3, 15, 10, 30), Precision.MINUTE),
        ("2024-03-15T10Z", _dt(2024, 3, 15, 10), Precision.HOUR),
        ("2024-03-15T12:00:00+02:00", _dt(2024, 3, 15, 10), Precision.SECOND),
    ],
)
def test_parse_bound_start_forms(text: str, instant: datetime, precision: Precision) -> None:
    bound = parse_bound(text)
    assert bound.instant == instant
    assert bound.precision is precision
    assert bound.end_after_period is False


@pytest.mark.parametrize(
    ("text", "instant", "precision"),
    [
        ("2024", _dt(2025), Precision.YEAR),
        ("2024-03", _dt(2024, 4), Precision.MONTH),
        ("2024-12", _dt(2025), Precision.MONTH),
        ("2024-03-15", _dt(2024, 3, 16), Precision.DAY),
        ("2024-03-15T10:30Z", _dt(2024, 3, 15, 10, 31), Precision.MINUTE),
        ("2024-03-15T10:00:00Z", _dt(2024, 3, 15, 10, 0, 1), Precision.SECOND),
    ],
)
def test_parse_bound_end_is_exclusive_bucket_end(
    text: str, instant: datetime, precision: Precision
) -> None:
    # "Left in 2024" -> valid_to = 2025-01-01, precision year (§3.1 example).
    bound = parse_bound(text, is_end=True)
    assert bound.instant == instant
    assert bound.precision is precision
    assert bound.end_after_period is True


def test_parse_bound_hour_with_half_hour_offset_keeps_its_period() -> None:
    # 10:00 at +05:30 is the period [04:30Z, 05:30Z), not the UTC hour
    # bucket [04:00Z, 05:00Z) (minor 5).
    start = parse_bound("2024-03-01T10+05:30")
    assert start.precision is Precision.HOUR
    assert start.instant == _dt(2024, 3, 1, 4, 30)
    end = parse_bound("2024-03-01T10+05:30", is_end=True)
    assert end.instant == _dt(2024, 3, 1, 5, 30)
    assert end.precision is Precision.HOUR


def test_parse_bound_unknown_and_invalid() -> None:
    assert parse_bound(None).is_unknown
    assert parse_bound("").is_unknown
    assert parse_bound("  ").precision is Precision.UNKNOWN
    with pytest.raises(ValueError):
        parse_bound("last year")
    with pytest.raises(ValueError):
        parse_bound("15/03/2024")
    with pytest.raises(ValueError):
        parse_bound("2024-13-40")


def test_bucket_round_trip() -> None:
    moment = _dt(2024, 3, 15, 10, 30, 45)
    assert bucket_start(moment, Precision.YEAR) == _dt(2024)
    assert bucket_end(moment, Precision.YEAR) == _dt(2025)
    assert bucket_end(moment, Precision.MONTH) == _dt(2024, 4)
    assert bucket_end(moment, Precision.DAY) == _dt(2024, 3, 16)
    assert bucket_end(moment, Precision.HOUR) == _dt(2024, 3, 15, 11)
    assert bucket_end(moment, Precision.MINUTE) == _dt(2024, 3, 15, 10, 31)
    assert bucket_end(moment, Precision.SECOND) == _dt(2024, 3, 15, 10, 30, 46)
    with pytest.raises(ValueError):
        bucket_end(moment, Precision.UNKNOWN)


def test_holds_at_edges() -> None:
    subject = uuid.uuid4()
    fact = make_fact(subject=subject, valid_from=_dt(2020), valid_to=_dt(2025))
    assert holds_at(fact, _dt(2020))  # start inclusive
    assert holds_at(fact, _dt(2024, 12, 31))
    assert not holds_at(fact, _dt(2025))  # end exclusive
    assert not holds_at(fact, _dt(2019, 12, 31))

    open_fact = make_fact(subject=subject)
    assert holds_at(open_fact, _dt(1900))
    assert holds_at(open_fact, _dt(2999))


def test_held_at_edges() -> None:
    subject = uuid.uuid4()
    invalidated = _dt(2026, 6, 1)
    fact = make_fact(
        subject=subject,
        recorded_at=T0,
        invalidated_at=invalidated,
    )
    assert not held_at(fact, _dt(2025, 12, 31))
    assert held_at(fact, T0)  # recorded inclusive
    assert held_at(fact, _dt(2026, 5, 31))
    assert not held_at(fact, invalidated)  # invalidated exclusive

    live = make_fact(subject=subject, recorded_at=T0)
    assert held_at(live, _dt(2999))
