"""Trigger mapping + matching for the automation engine (F21).

``trigger_type_for`` maps a raw producer event (board activity event type or a
workflow transition) to an :class:`AutomationTriggerType`. ``trigger_matches``
gates a rule's :class:`TriggerSpec` against an inbound envelope, honoring the
per-trigger config (notably ``to_state`` for ``WORKFLOW_STATE_CHANGED`` and the
optional ``to_status`` for ``TASK_STATUS_CHANGED``).
"""

from __future__ import annotations

from forge_board.automation.errors import UnknownTriggerError
from forge_board.automation.schemas import TriggerSpec
from forge_contracts.automation import (
    AutomationTriggerEnvelope,
    AutomationTriggerSource,
    AutomationTriggerType,
)

#: Board ``activity_events.event_type`` -> trigger type (best-effort; the board
#: event log is in-memory in the current foundation, so producers construct the
#: envelope directly — this mapping is the documented seam for when the
#: append-only board event log lands).
_BOARD_EVENT_MAP: dict[str, AutomationTriggerType] = {
    "task_created": AutomationTriggerType.TASK_CREATED,
    "task_status_changed": AutomationTriggerType.TASK_STATUS_CHANGED,
    "status_changed": AutomationTriggerType.TASK_STATUS_CHANGED,
    "task_assigned": AutomationTriggerType.TASK_ASSIGNED,
    "assignee_changed": AutomationTriggerType.TASK_ASSIGNED,
    "task_priority_changed": AutomationTriggerType.TASK_PRIORITY_CHANGED,
    "priority_changed": AutomationTriggerType.TASK_PRIORITY_CHANGED,
    "task_sla_breached": AutomationTriggerType.TASK_SLA_BREACHED,
    "sla_breached": AutomationTriggerType.TASK_SLA_BREACHED,
    "pr_merged": AutomationTriggerType.PR_MERGED,
    "approval_resolved": AutomationTriggerType.APPROVAL_RESOLVED,
}


def trigger_type_for(*, source: AutomationTriggerSource, event_type: str) -> AutomationTriggerType:
    """Map a producer event to its :class:`AutomationTriggerType`.

    Raises :class:`UnknownTriggerError` for an unmapped event type.
    """
    if source is AutomationTriggerSource.WORKFLOW_TRANSITION:
        return AutomationTriggerType.WORKFLOW_STATE_CHANGED
    mapped = _BOARD_EVENT_MAP.get(event_type)
    if mapped is None:
        raise UnknownTriggerError(event_type)
    return mapped


def trigger_matches(spec: TriggerSpec, envelope: AutomationTriggerEnvelope) -> bool:
    """True iff the rule's trigger spec matches the inbound envelope."""
    if spec.type != envelope.trigger_type:
        return False

    if spec.type is AutomationTriggerType.WORKFLOW_STATE_CHANGED:
        want = spec.config.get("to_state")
        if want is not None and envelope.change.get("to_state") != want:
            return False

    if spec.type is AutomationTriggerType.TASK_STATUS_CHANGED:
        want = spec.config.get("to_status")
        if want is not None and envelope.change.get("to_status") != want:
            return False

    if spec.type is AutomationTriggerType.TASK_PRIORITY_CHANGED:
        want = spec.config.get("to_priority")
        if want is not None and envelope.change.get("to_priority") != want:
            return False

    return True


__all__ = ["trigger_matches", "trigger_type_for"]
