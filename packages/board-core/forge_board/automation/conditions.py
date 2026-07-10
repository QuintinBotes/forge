"""Condition evaluation for the automation engine (F21), consolidated in F40.

The boolean predicate language is the shared, whitelisted primitive lifted into
the frozen contracts (:mod:`forge_contracts.conditions`) — F21 no longer carries
its own drifted copy. This module keeps the automation-specific *projection*:

* the whitelisted :data:`CONDITION_FIELDS` (task fields + derived ``has_spec`` +
  the trigger-local ``to_*``/``from_*`` change context + F40 aggregate fields);
* :func:`evaluate_condition`, which flattens an :class:`EntitySnapshot` into the
  ``fields`` mapping the shared evaluator consumes and then delegates to it.

Conditions are evaluated against the snapshot — never compiled to SQL — so user
input cannot reach the database.
"""

from __future__ import annotations

from typing import Any

from forge_board.automation.schemas import ConditionGroup, EntitySnapshot
from forge_contracts.conditions import evaluate_condition as _evaluate_shared

#: Whitelisted condition fields. Conforms to the real ``Task`` model (status is
#: an enum; no label/team tables). ``has_spec`` is derived; ``to_*``/``from_*``
#: read trigger-local change context; the ``*subtask*`` fields are F40 aggregates
#: derived over the entity's children (see :mod:`forge_board.automation.snapshot`).
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
        # F40 aggregate conditions (derived over the entity's subtasks).
        "all_subtasks_done",
        "subtask_count",
        "open_subtask_count",
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
    """A condition referenced a field outside :data:`CONDITION_FIELDS`.

    Retained for back-compat. The shared evaluator raises a plain
    :class:`ValueError` (a supertype) for the same failure, so callers that catch
    ``ValueError`` — as the engine does — are unaffected.
    """


def _flatten(snapshot: EntitySnapshot) -> dict[str, Any]:
    """Project a snapshot into the flat ``fields`` mapping the shared DSL reads.

    Mirrors the historical ``_resolve`` semantics exactly: ``has_spec`` is derived
    from ``spec_id``; the change-context fields come from ``snapshot.change``; every
    other whitelisted field comes from ``snapshot.fields``.
    """
    flat: dict[str, Any] = {}
    for field in CONDITION_FIELDS:
        if field == "has_spec":
            flat[field] = snapshot.fields.get("spec_id") is not None
        elif field in _CHANGE_FIELDS:
            flat[field] = snapshot.change.get(field)
        else:
            flat[field] = snapshot.fields.get(field)
    return flat


def evaluate_condition(group: ConditionGroup, snapshot: EntitySnapshot) -> bool:
    """Evaluate a (possibly nested) condition group against ``snapshot``.

    Delegates to the shared :func:`forge_contracts.conditions.evaluate_condition`
    with :data:`CONDITION_FIELDS` as the whitelist. An empty group is ``True``.

    Raises:
        ValueError: for an unknown field or a malformed operator value (e.g. an
            ``in`` without a list), raised fail-closed by the shared evaluator.
    """
    return _evaluate_shared(group, _flatten(snapshot), field_whitelist=CONDITION_FIELDS)


__all__ = ["CONDITION_FIELDS", "UnknownConditionFieldError", "evaluate_condition"]
