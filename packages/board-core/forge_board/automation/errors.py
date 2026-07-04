"""Engine-level errors for the automation rule engine (F21)."""

from __future__ import annotations


class RuleValidationError(Exception):
    """A rule failed structural/reference validation.

    Carries a list of ``{"path","code","message"}`` issues so the API can map it
    to a 422 with a machine-readable body.
    """

    def __init__(self, issues: list[dict[str, str]]) -> None:
        self.issues = issues
        super().__init__("; ".join(i.get("message", i.get("code", "?")) for i in issues))


class ActionForbiddenError(RuleValidationError):
    """A rule tried to perform a forbidden action (e.g. a human-gate event)."""

    def __init__(
        self,
        message: str,
        *,
        path: str = "actions",
        code: str = "action_forbidden_event",
    ) -> None:
        self.code = code
        super().__init__([{"path": path, "code": code, "message": message}])


class UnknownTriggerError(Exception):
    """No mapping exists from a domain event to an :class:`AutomationTriggerType`."""


class LoopAbortedError(Exception):
    """An evaluation was aborted by the loop guard (depth / causation cycle)."""


__all__ = [
    "ActionForbiddenError",
    "LoopAbortedError",
    "RuleValidationError",
    "UnknownTriggerError",
]
