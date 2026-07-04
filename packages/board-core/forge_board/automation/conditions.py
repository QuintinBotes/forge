"""Pure, in-memory condition evaluation for the automation engine (F21).

Conditions are evaluated against an :class:`EntitySnapshot` — never compiled to
SQL — so user input cannot reach the database. ``field`` names are whitelisted
(:data:`CONDITION_FIELDS`) and resolved against ``snapshot.fields`` /
``snapshot.change``.
"""

from __future__ import annotations

from typing import Any

from forge_board.automation.schemas import Condition, ConditionGroup, EntitySnapshot
from forge_contracts.automation import ConditionOp

#: Whitelisted condition fields. Conforms to the real ``Task`` model (status is
#: an enum; no label/team tables). ``has_spec`` is derived; ``to_*``/``from_*``
#: read trigger-local change context.
CONDITION_FIELDS: frozenset[str] = frozenset(
    {
        "status",
        "priority",
        "assignee_id",
        "kind",
        "epic_id",
        "sprint_id",
        "milestone_id",
        "estimate",
        "spec_id",
        "has_spec",
        "to_status",
        "from_status",
        "to_state",
        "to_priority",
        "from_priority",
        "approval_kind",
        "approval_status",
    }
)

#: Fields resolved from the trigger-local change context rather than the entity.
_CHANGE_FIELDS: frozenset[str] = frozenset(
    {
        "to_status",
        "from_status",
        "to_state",
        "to_priority",
        "from_priority",
        "approval_kind",
        "approval_status",
    }
)


class UnknownConditionFieldError(ValueError):
    """A condition referenced a field outside :data:`CONDITION_FIELDS`."""


def _resolve(field: str, snapshot: EntitySnapshot) -> Any:
    if field not in CONDITION_FIELDS:
        raise UnknownConditionFieldError(field)
    if field == "has_spec":
        return snapshot.fields.get("spec_id") is not None
    if field in _CHANGE_FIELDS:
        return snapshot.change.get(field)
    return snapshot.fields.get(field)


def _as_str(value: Any) -> Any:
    """Normalize enums to their value for stable comparison against JSON input."""
    return getattr(value, "value", value)


def _eval_condition(cond: Condition, snapshot: EntitySnapshot) -> bool:
    op = cond.op
    if cond.field == "changed":  # defensive; "changed" is an op, not a field
        raise UnknownConditionFieldError("changed")

    if op is ConditionOp.CHANGED:
        # True when the trigger-local change carries this field with a value.
        return _resolve(cond.field, snapshot) is not None

    actual = _as_str(_resolve(cond.field, snapshot))
    expected = _as_str(cond.value)

    if op is ConditionOp.IS_NULL:
        return actual is None
    if op is ConditionOp.IS_NOT_NULL:
        return actual is not None
    if op is ConditionOp.EQ:
        return actual == expected
    if op is ConditionOp.NE:
        return actual != expected
    if op in (ConditionOp.IN, ConditionOp.NOT_IN):
        if not isinstance(cond.value, list):
            raise ValueError(f"op {op.value} requires a list value")
        members = [_as_str(v) for v in cond.value]
        present = actual in members
        return present if op is ConditionOp.IN else not present
    if op in (ConditionOp.CONTAINS, ConditionOp.NOT_CONTAINS):
        container = actual if isinstance(actual, (list, tuple, set, str)) else []
        present = expected in container
        return present if op is ConditionOp.CONTAINS else not present
    if op in (ConditionOp.LT, ConditionOp.LTE, ConditionOp.GT, ConditionOp.GTE):
        if actual is None or expected is None:
            return False
        if op is ConditionOp.LT:
            return actual < expected
        if op is ConditionOp.LTE:
            return actual <= expected
        if op is ConditionOp.GT:
            return actual > expected
        return actual >= expected
    raise ValueError(f"unsupported op: {op}")  # pragma: no cover


def evaluate_condition(group: ConditionGroup, snapshot: EntitySnapshot) -> bool:
    """Evaluate a (possibly nested) condition group. Empty group == ``True``."""
    results: list[bool] = [_eval_condition(c, snapshot) for c in group.conditions]
    results.extend(evaluate_condition(g, snapshot) for g in group.groups)
    if not results:
        return True
    return all(results) if group.match == "all" else any(results)


__all__ = ["CONDITION_FIELDS", "UnknownConditionFieldError", "evaluate_condition"]
