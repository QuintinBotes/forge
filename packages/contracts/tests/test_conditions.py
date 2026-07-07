"""Tests for the shared condition DSL (F29 — ``forge_contracts.conditions``)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from forge_contracts.conditions import (
    Condition,
    ConditionGroup,
    ConditionOp,
    evaluate_condition,
)

WL = frozenset({"a", "b", "path", "branch", "now", "labels", "command"})


def _g(*conditions: Condition, match: str = "all") -> ConditionGroup:
    return ConditionGroup(match=match, conditions=list(conditions))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Structure (AC12)                                                            #
# --------------------------------------------------------------------------- #


def test_empty_group_true() -> None:
    assert evaluate_condition(ConditionGroup(), {}, field_whitelist=WL) is True


def test_all_vs_any() -> None:
    fields = {"a": 1, "b": 2}
    all_g = _g(
        Condition(field="a", op=ConditionOp.EQ, value=1),
        Condition(field="b", op=ConditionOp.EQ, value=99),
        match="all",
    )
    any_g = _g(
        Condition(field="a", op=ConditionOp.EQ, value=1),
        Condition(field="b", op=ConditionOp.EQ, value=99),
        match="any",
    )
    assert evaluate_condition(all_g, fields, field_whitelist=WL) is False
    assert evaluate_condition(any_g, fields, field_whitelist=WL) is True


def test_nested_groups() -> None:
    fields = {"a": 1, "b": 2}
    nested = ConditionGroup(
        match="all",
        conditions=[Condition(field="a", op=ConditionOp.EQ, value=1)],
        groups=[
            ConditionGroup(
                match="any",
                conditions=[
                    Condition(field="b", op=ConditionOp.EQ, value=0),
                    Condition(field="b", op=ConditionOp.EQ, value=2),
                ],
            )
        ],
    )
    assert evaluate_condition(nested, fields, field_whitelist=WL) is True


# --------------------------------------------------------------------------- #
# Ops table (AC13/AC14)                                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("op", "value", "actual", "expected"),
    [
        (ConditionOp.EQ, 1, 1, True),
        (ConditionOp.EQ, 1, 2, False),
        (ConditionOp.NE, 1, 2, True),
        (ConditionOp.NE, 1, 1, False),
        (ConditionOp.IN, [1, 2], 2, True),
        (ConditionOp.IN, [1, 2], 3, False),
        (ConditionOp.NOT_IN, [1, 2], 3, True),
        (ConditionOp.LT, 5, 4, True),
        (ConditionOp.LT, 5, 5, False),
        (ConditionOp.LTE, 5, 5, True),
        (ConditionOp.GT, 5, 6, True),
        (ConditionOp.GTE, 5, 5, True),
        (ConditionOp.CONTAINS, "apply", "terraform apply -auto", True),
        (ConditionOp.CONTAINS, "x", "abc", False),
        (ConditionOp.NOT_CONTAINS, "x", "abc", True),
        (ConditionOp.MATCHES_GLOB, "infra/**", "infra/x.tf", True),
        (ConditionOp.MATCHES_GLOB, "infra/**", "app/x.py", False),
    ],
)
def test_ops_table(op: ConditionOp, value: object, actual: object, expected: bool) -> None:
    cond = Condition(field="a", op=op, value=value)
    assert evaluate_condition(_g(cond), {"a": actual}, field_whitelist=WL) is expected


def test_is_null_and_is_not_null() -> None:
    assert (
        evaluate_condition(_g(Condition(field="a", op=ConditionOp.IS_NULL)), {}, field_whitelist=WL)
        is True
    )
    assert (
        evaluate_condition(
            _g(Condition(field="a", op=ConditionOp.IS_NOT_NULL)),
            {"a": 1},
            field_whitelist=WL,
        )
        is True
    )


def test_contains_membership_on_list() -> None:
    cond = Condition(field="labels", op=ConditionOp.CONTAINS, value="urgent")
    assert evaluate_condition(_g(cond), {"labels": ["urgent", "p1"]}, field_whitelist=WL) is True


# --------------------------------------------------------------------------- #
# Validation (AC14)                                                           #
# --------------------------------------------------------------------------- #


def test_field_not_in_whitelist_raises() -> None:
    cond = Condition(field="nope", op=ConditionOp.EQ, value=1)
    with pytest.raises(ValueError, match="whitelist"):
        evaluate_condition(_g(cond), {"nope": 1}, field_whitelist=WL)


def test_in_op_requires_list() -> None:
    cond = Condition(field="a", op=ConditionOp.IN, value="not-a-list")
    with pytest.raises(ValueError, match="requires a list"):
        evaluate_condition(_g(cond), {"a": 1}, field_whitelist=WL)


def test_time_window_requires_mapping() -> None:
    cond = Condition(field="now", op=ConditionOp.IN_TIME_WINDOW, value="bad")
    with pytest.raises(ValueError, match="mapping value"):
        evaluate_condition(
            _g(cond), {"now": datetime(2026, 6, 23, 12, tzinfo=UTC)}, field_whitelist=WL
        )


def test_time_window_non_datetime_operand_raises() -> None:
    cond = Condition(
        field="now",
        op=ConditionOp.IN_TIME_WINDOW,
        value={"days": [0, 1, 2, 3, 4], "start": "09:00", "end": "17:00"},
    )
    with pytest.raises(ValueError, match="must be a datetime"):
        evaluate_condition(_g(cond), {"now": "not-a-dt"}, field_whitelist=WL)


# --------------------------------------------------------------------------- #
# Missing-field semantics (fail-closed)                                       #
# --------------------------------------------------------------------------- #


def test_missing_field_positive_ops_false() -> None:
    for op in (ConditionOp.EQ, ConditionOp.IN, ConditionOp.CONTAINS, ConditionOp.MATCHES_GLOB):
        value: object = [1] if op is ConditionOp.IN else "x"
        cond = Condition(field="a", op=op, value=value)
        assert evaluate_condition(_g(cond), {}, field_whitelist=WL) is False


def test_missing_field_negative_ops_true() -> None:
    for op, value in (
        (ConditionOp.NE, "x"),
        (ConditionOp.NOT_IN, ["x"]),
        (ConditionOp.NOT_CONTAINS, "x"),
    ):
        cond = Condition(field="a", op=op, value=value)
        assert evaluate_condition(_g(cond), {}, field_whitelist=WL) is True


# --------------------------------------------------------------------------- #
# Time window correctness (AC13)                                              #
# --------------------------------------------------------------------------- #

_BUSINESS = {"days": [0, 1, 2, 3, 4], "start": "09:00", "end": "17:00", "tz": "UTC"}
_TUESDAY_NOON = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)  # Tue
_SATURDAY_NOON = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)  # Sat


def test_in_time_window_weekday() -> None:
    in_w = Condition(field="now", op=ConditionOp.IN_TIME_WINDOW, value=_BUSINESS)
    assert evaluate_condition(_g(in_w), {"now": _TUESDAY_NOON}, field_whitelist=WL) is True
    assert evaluate_condition(_g(in_w), {"now": _SATURDAY_NOON}, field_whitelist=WL) is False
    assert evaluate_condition(_g(in_w), {"now": None}, field_whitelist=WL) is False


def test_not_in_time_window_is_inverse_and_failclosed() -> None:
    out_w = Condition(field="now", op=ConditionOp.NOT_IN_TIME_WINDOW, value=_BUSINESS)
    assert evaluate_condition(_g(out_w), {"now": _TUESDAY_NOON}, field_whitelist=WL) is False
    assert evaluate_condition(_g(out_w), {"now": _SATURDAY_NOON}, field_whitelist=WL) is True
    # Missing clock fails CLOSED for an outside-the-window gate.
    assert evaluate_condition(_g(out_w), {"now": None}, field_whitelist=WL) is True
