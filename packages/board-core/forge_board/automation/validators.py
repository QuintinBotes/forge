"""Rule validation for the automation engine (F21).

``validate_rule`` hard-fails (raises :class:`RuleValidationError` /
:class:`ActionForbiddenError`) on structural and reference errors and returns a
list of non-fatal :class:`RuleWarning`\\s (notably ``possible_self_trigger``).

Double-enforcement of the human-gate guard: a ``SEND_WORKFLOW_EVENT`` whose
event is in ``HUMAN_GATE_EVENTS`` is rejected here *and* at run time by the
executor, so a rule crafted via direct DB insert still cannot approve anything.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from forge_board.automation.conditions import CONDITION_FIELDS
from forge_board.automation.errors import ActionForbiddenError, RuleValidationError
from forge_board.automation.schemas import (
    AutomationRuleSpec,
    Condition,
    ConditionGroup,
    SendWorkflowEventAction,
    SetStatusAction,
)
from forge_contracts.automation import (
    HUMAN_GATE_EVENTS,
    AutomationActionType,
    AutomationTriggerType,
)


@dataclass
class RuleRefContext:
    """Project references the validator checks rule fields against.

    Empty sets disable the corresponding reference check (used in YAML/round-trip
    validation where the project graph is not loaded).
    """

    valid_assignee_ids: frozenset[uuid.UUID] = frozenset()
    valid_sprint_ids: frozenset[uuid.UUID] = frozenset()
    valid_milestone_ids: frozenset[uuid.UUID] = frozenset()
    check_references: bool = False


@dataclass
class RuleWarning:
    code: str
    message: str


#: Trigger types that require specific config keys.
_REQUIRED_TRIGGER_CONFIG: dict[AutomationTriggerType, tuple[str, ...]] = {
    AutomationTriggerType.WORKFLOW_STATE_CHANGED: ("to_state",),
    AutomationTriggerType.SCHEDULED: ("cron",),
}

#: One standard cron field: ``*``/number, optional ``-b`` range, optional ``/n``
#: step, comma-listed. Grammar-only (the worker's Celery parser is authoritative
#: and fault-isolated per rule at dispatch); this rejects garbage at save time so
#: a malformed cron never reaches persistence.
_CRON_FIELD = re.compile(r"^(\*|\d+)(-\d+)?(/\d+)?(,(\*|\d+)(-\d+)?(/\d+)?)*$")


def _is_well_formed_cron(expr: object) -> bool:
    """True iff ``expr`` is a well-formed 5-field cron string (``m h dom mon dow``)."""
    if not isinstance(expr, str):
        return False
    fields = expr.split()
    return len(fields) == 5 and all(_CRON_FIELD.match(f) for f in fields)


def _collect_condition_fields(group: ConditionGroup, out: list[Condition]) -> None:
    out.extend(group.conditions)
    for sub in group.groups:
        _collect_condition_fields(sub, out)


def validate_rule(spec: AutomationRuleSpec, ctx: RuleRefContext | None = None) -> list[RuleWarning]:
    """Validate a rule; raise on hard errors, return non-fatal warnings."""
    ctx = ctx or RuleRefContext()
    issues: list[dict[str, str]] = []

    # 1. Trigger config completeness.
    required = _REQUIRED_TRIGGER_CONFIG.get(spec.trigger.type, ())
    for key in required:
        if spec.trigger.config.get(key) in (None, ""):
            issues.append(
                {
                    "path": f"trigger.config.{key}",
                    "code": "missing_trigger_config",
                    "message": f"trigger {spec.trigger.type.value} requires config '{key}'",
                }
            )

    # 1a. Scheduled cron well-formedness: reject a malformed expression at save
    #     time (defence-in-depth; the dispatch loop also isolates a bad cron per
    #     rule so one tenant's garbage never denies scheduled service to others).
    if spec.trigger.type is AutomationTriggerType.SCHEDULED:
        cron = spec.trigger.config.get("cron")
        if cron not in (None, "") and not _is_well_formed_cron(cron):
            issues.append(
                {
                    "path": "trigger.config.cron",
                    "code": "malformed_cron",
                    "message": f"cron expression {cron!r} is not a valid 5-field cron string",
                }
            )

    # 2. Condition field whitelist.
    conditions: list[Condition] = []
    _collect_condition_fields(spec.condition, conditions)
    for cond in conditions:
        if cond.field not in CONDITION_FIELDS:
            issues.append(
                {
                    "path": f"condition.{cond.field}",
                    "code": "unknown_condition_field",
                    "message": f"condition field '{cond.field}' is not allowed",
                }
            )

    # 3. Per-action checks. The human-gate guard raises immediately (its own
    #    error code) so the API maps it to the dedicated 422 body.
    for action in spec.actions:
        if isinstance(action, SendWorkflowEventAction) and action.event in HUMAN_GATE_EVENTS:
            raise ActionForbiddenError(
                f"automations cannot send the human-gate event '{action.event}'"
            )
        if isinstance(action, SetStatusAction) and action.status is None:  # pragma: no cover
            issues.append(
                {
                    "path": "actions.set_status.status",
                    "code": "missing_status",
                    "message": "set_status requires a status",
                }
            )

    # 4. Reference checks (only when the project graph is provided).
    if ctx.check_references:
        for action in spec.actions:
            ref_id = getattr(action, "assignee_id", None)
            if (
                action.type is AutomationActionType.SET_ASSIGNEE
                and ref_id is not None
                and ref_id not in ctx.valid_assignee_ids
            ):
                issues.append(
                    {
                        "path": "actions.set_assignee.assignee_id",
                        "code": "unknown_reference",
                        "message": f"assignee {ref_id} is not in the project",
                    }
                )
            if action.type is AutomationActionType.SET_FIELD:
                self_field = getattr(action, "field", None)
                value = getattr(action, "value", None)
                if (
                    self_field == "sprint_id"
                    and value is not None
                    and _as_uuid(value) not in ctx.valid_sprint_ids
                ):
                    issues.append(
                        {
                            "path": "actions.set_field.value",
                            "code": "unknown_reference",
                            "message": f"sprint {value} is not in the project",
                        }
                    )
                if (
                    self_field == "milestone_id"
                    and value is not None
                    and _as_uuid(value) not in ctx.valid_milestone_ids
                ):
                    issues.append(
                        {
                            "path": "actions.set_field.value",
                            "code": "unknown_reference",
                            "message": f"milestone {value} is not in the project",
                        }
                    )

    if issues:
        raise RuleValidationError(issues)

    return _warnings(spec)


def _as_uuid(value: object) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


def _warnings(spec: AutomationRuleSpec) -> list[RuleWarning]:
    warnings: list[RuleWarning] = []

    # possible_self_trigger: a SET_STATUS action under a status-change trigger
    # with no narrowing condition can re-fire its own trigger.
    has_status_action = any(a.type is AutomationActionType.SET_STATUS for a in spec.actions)
    is_status_trigger = spec.trigger.type in (
        AutomationTriggerType.TASK_STATUS_CHANGED,
        AutomationTriggerType.WORKFLOW_STATE_CHANGED,
    )
    no_condition = not spec.condition.conditions and not spec.condition.groups
    if has_status_action and is_status_trigger and no_condition:
        warnings.append(
            RuleWarning(
                code="possible_self_trigger",
                message=(
                    "a set_status action under a status-change trigger with no "
                    "narrowing condition can re-trigger this rule"
                ),
            )
        )

    return warnings


__all__ = ["RuleRefContext", "RuleWarning", "validate_rule"]
