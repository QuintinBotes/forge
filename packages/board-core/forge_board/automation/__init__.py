"""Pure automation rule engine (F21).

Extends ``forge_board`` (the board domain layer) with the declarative
``WHEN trigger IF condition THEN actions`` rule engine described in the F21
slice. The engine is side-effect-free except through an injected
:class:`ActionExecutor`; persistence + the concrete board/workflow executor live
in the API / worker layers.

This lives under ``forge_board`` (rather than a separate ``forge_automation``
package) to honor the repo's "extend existing packages, do not add parallel
packages" rule. The public symbols match the slice's documented surface.
"""

from __future__ import annotations

from forge_board.automation.conditions import (
    CONDITION_FIELDS,
    UnknownConditionFieldError,
    evaluate_condition,
)
from forge_board.automation.engine import AutomationEngine
from forge_board.automation.errors import (
    ActionForbiddenError,
    LoopAbortedError,
    RuleValidationError,
    UnknownTriggerError,
)
from forge_board.automation.executor import (
    ActionContext,
    ActionExecutor,
    RecordingActionExecutor,
)
from forge_board.automation.loop_guard import (
    DEFAULT_MAX_DEPTH,
    LoopGuard,
    resolve_max_depth,
)
from forge_board.automation.schemas import (
    ActionResult,
    ActionSpec,
    AddCommentAction,
    AutomationRuleSpec,
    AutomationRuleSpecWithMeta,
    CloseLinkedSpecTasksAction,
    Condition,
    ConditionGroup,
    CreateTaskAction,
    EntitySnapshot,
    ExecutionResult,
    SendNotificationAction,
    SendWorkflowEventAction,
    SetAssigneeAction,
    SetFieldAction,
    SetPriorityAction,
    SetStatusAction,
    TriggerSpec,
)
from forge_board.automation.snapshot import snapshot_for_task
from forge_board.automation.triggers import trigger_matches, trigger_type_for
from forge_board.automation.validators import (
    RuleRefContext,
    RuleWarning,
    validate_rule,
)

__all__ = [
    "CONDITION_FIELDS",
    "DEFAULT_MAX_DEPTH",
    "ActionContext",
    "ActionExecutor",
    "ActionForbiddenError",
    "ActionResult",
    "ActionSpec",
    "AddCommentAction",
    "AutomationEngine",
    "AutomationRuleSpec",
    "AutomationRuleSpecWithMeta",
    "CloseLinkedSpecTasksAction",
    "Condition",
    "ConditionGroup",
    "CreateTaskAction",
    "EntitySnapshot",
    "ExecutionResult",
    "LoopAbortedError",
    "LoopGuard",
    "RecordingActionExecutor",
    "RuleRefContext",
    "RuleValidationError",
    "RuleWarning",
    "SendNotificationAction",
    "SendWorkflowEventAction",
    "SetAssigneeAction",
    "SetFieldAction",
    "SetPriorityAction",
    "SetStatusAction",
    "TriggerSpec",
    "UnknownConditionFieldError",
    "UnknownTriggerError",
    "evaluate_condition",
    "resolve_max_depth",
    "snapshot_for_task",
    "trigger_matches",
    "trigger_type_for",
    "validate_rule",
]
