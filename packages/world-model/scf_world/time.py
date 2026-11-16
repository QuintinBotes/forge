"""Two-clock time: world bounds with precision, and the hold/held predicates.

Pure module — no I/O. :func:`holds_at` (world clock) and :func:`held_at`
(record clock) are the *only* Python implementations of the two predicates;
the repository carries the single SQL rendering of each. A world bound is an
instant plus a :class:`Precision`: a ``valid_from`` is stored at the *start*
of its bucket, a ``valid_to`` at the *exclusive end* of its bucket, so
"left in 2024" is ``valid_to = 2025-01-01T00:00Z, precision = year``.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Precision(enum.StrEnum):
    """How precisely a world bound is known (the precision ladder)."""

    YEAR = "year"
    MONTH = "month"
    DAY = "day"
    HOUR = "hour"
    MINUTE = "minute"
    SECOND = "second"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class WorldBound:
    """One end of a fact's world interval: an instant plus its precision.

    ``unknown`` precision always pairs with ``instant is None``.
    ``end_after_period`` records that the stored instant is the exclusive end
    of the stated period (every parsed ``valid_to``), as opposed to a derived
    end set to a successor's start.
    """

    instant: datetime | None
    precision: Precision
    end_after_period: bool = False

    @classmethod
    def unknown(cls) -> WorldBound:
        """The open/unknown bound (stored as NULL)."""
        return cls(instant=None, precision=Precision.UNKNOWN)

    @property
    def is_unknown(self) -> bool:
        return self.instant is None


def ensure_utc(value: datetime) -> datetime:
    """Return ``value`` as an aware UTC datetime (naive is assumed UTC).

    SQLite returns naive datetimes for ``DateTime(timezone=True)`` columns;
    normalising at every boundary keeps comparisons total on both dialects.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def bucket_start(instant: datetime, precision: Precision) -> datetime:
    """Truncate ``instant`` to the start of its ``precision`` bucket (UTC)."""
    value = ensure_utc(instant)
    match precision:
        case Precision.YEAR:
            return value.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        case Precision.MONTH:
            return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        case Precision.DAY:
            return value.replace(hour=0, minute=0, second=0, microsecond=0)
        case Precision.HOUR:
            return value.replace(minute=0, second=0, microsecond=0)
        case Precision.MINUTE:
            return value.replace(second=0, microsecond=0)
        case Precision.SECOND:
            return value.replace(microsecond=0)
        case Precision.UNKNOWN:
            raise ValueError("an unknown-precision bound has no bucket")


def period_end(start: datetime, precision: Precision) -> datetime:
    """The exclusive end of the period *beginning* at ``start`` (UTC).

    Unlike :func:`bucket_end` this never truncates first, so a period that
    begins off the UTC grid (an hour stated with a +05:30 offset begins at
    :30 UTC) keeps its true length and phase.
    """
    value = ensure_utc(start)
    match precision:
        case Precision.YEAR:
            return value.replace(year=value.year + 1)
        case Precision.MONTH:
            if value.month == 12:
                return value.replace(year=value.year + 1, month=1)
            return value.replace(month=value.month + 1)
        case Precision.DAY:
            return value + timedelta(days=1)
        case Precision.HOUR:
            return value + timedelta(hours=1)
        case Precision.MINUTE:
            return value + timedelta(minutes=1)
        case Precision.SECOND:
            return value + timedelta(seconds=1)
        case Precision.UNKNOWN:
            raise ValueError("an unknown-precision bound has no bucket")


def bucket_end(instant: datetime, precision: Precision) -> datetime:
    """The exclusive end of the bucket containing ``instant`` (UTC)."""
    return period_end(bucket_start(instant, precision), precision)


_YEAR_RE = re.compile(r"^(\d{4})$")
_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_DAY_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
# An ISO time-of-day tail: THH, THH:MM or THH:MM:SS (space separator allowed).
_TIME_TAIL_RE = re.compile(r"[Tt ](\d{2})(?::(\d{2}))?(?::(\d{2}))?")


def parse_bound(text: str | None, *, is_end: bool = False) -> WorldBound:
    """Parse a stated time into a stored :class:`WorldBound`.

    Accepts ``YYYY``, ``YYYY-MM``, ``YYYY-MM-DD`` or a full ISO-8601 instant;
    precision comes from the form. A start bound is stored at its bucket
    start; an end bound (``is_end=True``) at its bucket's exclusive end with
    ``end_after_period=True``. ``None``/empty yields the unknown bound.
    Anything else (relative words like "last year", garbage) raises
    :class:`ValueError` — callers that accept relative words map that to
    unknown themselves.
    """
    if text is None or not text.strip():
        return WorldBound.unknown()
    value = text.strip()

    start: datetime
    if match := _YEAR_RE.match(value):
        precision = Precision.YEAR
        start = datetime(int(match.group(1)), 1, 1, tzinfo=UTC)
    elif match := _MONTH_RE.match(value):
        precision = Precision.MONTH
        start = datetime(int(match.group(1)), int(match.group(2)), 1, tzinfo=UTC)
    elif match := _DAY_RE.match(value):
        precision = Precision.DAY
        start = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=UTC)
    else:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"unrecognised time bound: {text!r}") from exc
        tail = _TIME_TAIL_RE.search(value)
        if tail is None:  # a date form fromisoformat accepts but we do not
            raise ValueError(f"unrecognised time bound: {text!r}")
        if tail.group(3) is not None:
            precision = Precision.SECOND
        elif tail.group(2) is not None:
            precision = Precision.MINUTE
        else:
            precision = Precision.HOUR
        # Truncate in the stated offset's own frame, then shift to UTC: an
        # hour stated as 10:00+05:30 is the period [04:30Z, 05:30Z), not
        # the UTC hour bucket [04:00Z, 05:00Z).
        start = bucket_start(parsed.replace(tzinfo=UTC), precision) - (
            parsed.utcoffset() or timedelta(0)
        )

    if is_end:
        return WorldBound(
            instant=period_end(start, precision), precision=precision, end_after_period=True
        )
    return WorldBound(instant=start, precision=precision)


def bound_from_stored(
    instant: datetime | None, precision: str, *, end_after_period: bool = False
) -> WorldBound:
    """Rebuild a :class:`WorldBound` from stored columns (no re-bucketing)."""
    if instant is None:
        return WorldBound.unknown()
    return WorldBound(
        instant=ensure_utc(instant),
        precision=Precision(precision),
        end_after_period=end_after_period,
    )


class FactTimes(Protocol):
    """The four timestamps the two predicates read (structural, no import)."""

    valid_from: datetime | None
    valid_to: datetime | None
    recorded_at: datetime
    invalidated_at: datetime | None


def holds_at(fact: FactTimes, at: datetime) -> bool:
    """World clock: was the fact true at instant ``at``?

    ``(valid_from is None or valid_from <= at) and (valid_to is None or at <
    valid_to)`` — start-inclusive, end-exclusive.
    """
    moment = ensure_utc(at)
    if fact.valid_from is not None and ensure_utc(fact.valid_from) > moment:
        return False
    return fact.valid_to is None or moment < ensure_utc(fact.valid_to)


def held_at(fact: FactTimes, as_of: datetime) -> bool:
    """Record clock: was the fact believed as of instant ``as_of``?

    ``recorded_at <= as_of and (invalidated_at is None or as_of <
    invalidated_at)``.
    """
    moment = ensure_utc(as_of)
    if ensure_utc(fact.recorded_at) > moment:
        return False
    return fact.invalidated_at is None or moment < ensure_utc(fact.invalidated_at)


def intervals_overlap(
    a_from: datetime | None,
    a_to: datetime | None,
    b_from: datetime | None,
    b_to: datetime | None,
) -> bool:
    """True iff two ``[from, to)`` world intervals share an instant.

    A missing start is -infinity, a missing end +infinity; touching at an
    endpoint (``a_to == b_from``) does not overlap.
    """
    if a_to is not None and b_from is not None and ensure_utc(b_from) >= ensure_utc(a_to):
        return False
    return not (b_to is not None and a_from is not None and ensure_utc(a_from) >= ensure_utc(b_to))


__all__ = [
    "FactTimes",
    "Precision",
    "WorldBound",
    "bound_from_stored",
    "bucket_end",
    "bucket_start",
    "ensure_utc",
    "held_at",
    "holds_at",
    "intervals_overlap",
    "parse_bound",
    "period_end",
]
