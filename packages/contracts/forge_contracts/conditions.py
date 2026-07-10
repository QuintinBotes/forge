"""Shared, deterministic condition DSL (F29 — advanced policy engine).

A small, **whitelisted**, non-Turing-complete boolean predicate language lifted
into the frozen contracts so the policy engine (``forge_policy``) and, in future,
the F21 automation engine can share one implementation (see the F29 slice §12).

Design invariants (mirrors the F21 automation engine and the deterministic
Supervisor — *no LLM, ever*):

* **Pure & total.** :func:`evaluate_condition` performs no I/O, reads no wall
  clock (the evaluation clock is supplied as a field, e.g. ``now``), and returns
  a ``bool`` for every input — except for the explicit, fail-closed
  :class:`ValueError` validation raises documented below.
* **Whitelisted fields.** Every ``Condition.field`` must be in the caller's
  ``field_whitelist`` or evaluation raises :class:`ValueError` — a typo can never
  silently disable a rule.
* **Fail-closed missing fields.** A field absent from ``fields`` (or ``None`` —
  e.g. an unset clock) is treated as ``None``: *positive* ops
  (``eq``/``in``/``contains``/``matches_glob``/``lt``…/``in_time_window``) →
  ``False``; ``is_null`` → ``True``; the *negative* ops
  (``ne``/``not_in``/``not_contains``/``is_not_null``/``not_in_time_window``) →
  ``True`` (so a missing clock fails CLOSED for an "outside-the-window" gate).

This module is intentionally dependency-free (stdlib + pydantic only) so it can
live in ``forge_contracts``.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class ConditionOp(StrEnum):
    """The whitelisted predicate operators (evaluated in-memory; never SQL)."""

    EQ = "eq"
    NE = "ne"
    IN = "in"
    NOT_IN = "not_in"
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"
    #: True iff the operand is present (non-``None``). The F21 automation engine
    #: uses this against trigger-local change fields (e.g. ``to_status changed``);
    #: in the flat-field model "changed" is exactly "present". Value is ignored.
    CHANGED = "changed"
    #: gitwildmatch-ish (fnmatch) glob for path/branch values; value: str | list[str].
    MATCHES_GLOB = "matches_glob"
    #: operand must be a datetime (e.g. ``now``); value:
    #: ``{days:[0..6], start:"HH:MM", end:"HH:MM", tz:"UTC"}``.
    IN_TIME_WINDOW = "in_time_window"
    #: boolean inverse of IN_TIME_WINDOW (same value shape); gates "outside the window".
    NOT_IN_TIME_WINDOW = "not_in_time_window"


#: Ops whose fail-closed value on a ``None`` operand is ``True``.
_NEGATIVE_OPS: frozenset[ConditionOp] = frozenset(
    {
        ConditionOp.NE,
        ConditionOp.NOT_IN,
        ConditionOp.NOT_CONTAINS,
        ConditionOp.IS_NOT_NULL,
        ConditionOp.NOT_IN_TIME_WINDOW,
    }
)


class Condition(BaseModel):
    """A single ``field <op> value`` predicate."""

    field: str
    op: ConditionOp
    value: Any = None


class ConditionGroup(BaseModel):
    """A nested all/any tree of conditions. An empty group is always ``True``."""

    match: Literal["all", "any"] = "all"
    conditions: list[Condition] = Field(default_factory=list)
    groups: list[ConditionGroup] = Field(default_factory=list)


def _parse_hhmm(value: Any) -> int:
    """Parse an ``"HH:MM"`` string into minutes-since-midnight."""
    if not isinstance(value, str) or ":" not in value:
        raise ValueError(f"time window bound must be 'HH:MM', got {value!r}")
    hh, mm = value.split(":", 1)
    return int(hh) * 60 + int(mm)


def _in_time_window(moment: datetime, window: Mapping[str, Any]) -> bool:
    """True iff ``moment`` (normalised to UTC) is inside ``window``.

    ``window`` is ``{days?:[0..6] (0=Mon), start:"HH:MM", end:"HH:MM", tz?:"UTC"}``.
    The window is ``[start, end)`` and only UTC is supported (the operand is
    converted to UTC; a naive datetime is assumed UTC).
    """
    start = _parse_hhmm(window["start"])
    end = _parse_hhmm(window["end"])
    days = window.get("days")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    moment = moment.astimezone(UTC)
    if days is not None and moment.weekday() not in set(days):
        return False
    minutes = moment.hour * 60 + moment.minute
    return start <= minutes < end


def _contains(actual: Any, value: Any) -> bool:
    if isinstance(actual, str):
        return str(value) in actual
    if isinstance(actual, (list, tuple, set, frozenset)):
        return value in actual
    return False


def _matches_glob(actual: Any, value: Any) -> bool:
    patterns = [value] if isinstance(value, str) else list(value or [])
    text = str(actual).replace("\\", "/").lstrip("/")
    base = text.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(text, p) or fnmatch.fnmatch(base, p) for p in patterns)


def _validate_value_shape(op: ConditionOp, value: Any) -> None:
    """Fail-closed value-shape checks raised regardless of operand presence."""
    if op in (ConditionOp.IN, ConditionOp.NOT_IN) and not isinstance(value, list):
        raise ValueError(f"op {op.value!r} requires a list value, got {type(value).__name__}")
    if op in (ConditionOp.IN_TIME_WINDOW, ConditionOp.NOT_IN_TIME_WINDOW) and (
        not isinstance(value, Mapping) or "start" not in value or "end" not in value
    ):
        raise ValueError(f"op {op.value!r} requires a {{start, end, days?, tz?}} mapping value")


def _eval_condition(cond: Condition, fields: Mapping[str, Any], whitelist: frozenset[str]) -> bool:
    if cond.field not in whitelist:
        raise ValueError(f"condition field {cond.field!r} is not in the field whitelist")

    op = cond.op
    value = cond.value
    _validate_value_shape(op, value)

    actual = fields.get(cond.field)

    if op is ConditionOp.IS_NULL:
        return actual is None
    if op is ConditionOp.IS_NOT_NULL:
        return actual is not None
    if op is ConditionOp.CHANGED:
        # "changed" == "present" in the flat-field model (the automation engine
        # projects trigger-local change fields into ``fields``).
        return actual is not None

    if actual is None:
        # Missing/None operand: positive ops False, negative ops True (fail-closed).
        return op in _NEGATIVE_OPS

    if op is ConditionOp.EQ:
        return actual == value
    if op is ConditionOp.NE:
        return actual != value
    if op is ConditionOp.IN:
        return actual in value
    if op is ConditionOp.NOT_IN:
        return actual not in value
    if op is ConditionOp.CONTAINS:
        return _contains(actual, value)
    if op is ConditionOp.NOT_CONTAINS:
        return not _contains(actual, value)
    if op in (ConditionOp.LT, ConditionOp.LTE, ConditionOp.GT, ConditionOp.GTE):
        if value is None:
            return False
        try:
            if op is ConditionOp.LT:
                return actual < value
            if op is ConditionOp.LTE:
                return actual <= value
            if op is ConditionOp.GT:
                return actual > value
            return actual >= value
        except TypeError:
            return False  # totality: incomparable types never crash the gate
    if op is ConditionOp.MATCHES_GLOB:
        return _matches_glob(actual, value)
    # Time-window ops (operand is non-None here).
    if not isinstance(actual, datetime):
        raise ValueError(f"op {op.value!r} operand must be a datetime, got {type(actual).__name__}")
    inside = _in_time_window(actual, value)
    return inside if op is ConditionOp.IN_TIME_WINDOW else not inside


def evaluate_condition(
    group: ConditionGroup,
    fields: Mapping[str, Any],
    *,
    field_whitelist: frozenset[str],
) -> bool:
    """Pure boolean evaluation of a (possibly nested) ``group`` against ``fields``.

    Raises:
        ValueError: if any ``Condition.field`` is outside ``field_whitelist``, an
            ``in``/``not_in`` value is not a list, a time-window value lacks the
            ``{start, end}`` shape, or a time-window operand is a non-``None``
            non-datetime.

    An empty group (no conditions and no sub-groups) evaluates to ``True``; an
    ``"all"`` group over no results is ``True`` and an ``"any"`` group over no
    results is ``False`` (vacuous-truth convention matching the F21 engine).
    """
    results: list[bool] = [_eval_condition(c, fields, field_whitelist) for c in group.conditions]
    results.extend(
        evaluate_condition(g, fields, field_whitelist=field_whitelist) for g in group.groups
    )
    if not results:
        return True
    return all(results) if group.match == "all" else any(results)


__all__ = [
    "Condition",
    "ConditionGroup",
    "ConditionOp",
    "evaluate_condition",
]
