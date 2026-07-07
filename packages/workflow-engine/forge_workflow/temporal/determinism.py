"""Pure, IO-free DSL transition evaluator for the Temporal workflow sandbox (F25).

Temporal workflow code must be deterministic and side-effect-free, so the F07
guards that read the database cannot run inside the workflow. This module is the
*pure* subset of the FSM engine: it resolves a ``(state, event)`` pair against a
:class:`~forge_contracts.WorkflowDefinition` using **only** data already present
inside the workflow (a :class:`PureGuardContext` carrying retry budget, check
results, confidence, merge signals, and the ``GuardInputs`` an Activity loaded).

It imports **no** clock / random / DB / network module — guaranteeing the
workflow body replays deterministically (verified by ``test_replay.py`` and the
import-purity assertion in ``test_determinism.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from forge_contracts import WorkflowDefinition, WorkflowState, WorkflowTransition
from forge_workflow.exceptions import (
    GuardFailedError,
    InvalidTransitionError,
    PreconditionError,
)
from forge_workflow.fsm import (
    RETRY_BUDGET_EXHAUSTED,
    RETRY_BUDGET_REMAINING,
)

#: Guard tokens whose truth is computed from the pure context (not the DB).
_PURE_GUARDS = frozenset(
    {
        "all_checks_passed",
        "checks_failed",
        "low_confidence",
        "ci_status_green",
        "spec_validated",
        "review_approved_by_human",
    }
)


@dataclass
class PureGuardContext:
    """Only data already inside the workflow — never a DB read (F25)."""

    retry_count: int = 0
    max_retries: int = 3
    checks: dict[str, bool] | None = None
    confidence: float | None = None
    confidence_threshold: float = 0.72
    #: review_approved_by_human / ci_status_green / spec_validated (from payload).
    merge_signals: dict[str, bool] | None = None
    #: from the load_guard_inputs Activity: preconditions + plan flag.
    preconditions: dict[str, bool] | None = None
    plan_required: bool = True

    def as_flags(self) -> dict[str, Any]:
        """Flatten into the boolean flag map the guard evaluator reads."""
        checks = self.checks or {}
        all_passed = bool(checks) and all(checks.values())
        flags: dict[str, Any] = {
            "all_checks_passed": all_passed,
            "checks_failed": bool(checks) and not all_passed,
            "low_confidence": (
                self.confidence is not None and self.confidence < self.confidence_threshold
            ),
        }
        for key, value in (self.merge_signals or {}).items():
            flags[key] = bool(value)
        return flags


@dataclass
class TransitionDecision:
    """The resolved transition for a ``(state, event)`` pair."""

    to_state: WorkflowState
    guard_results: dict[str, bool] = field(default_factory=dict)
    effects: list[str] = field(default_factory=list)
    record: str | None = None
    skill: str | None = None


def _triggers(t: WorkflowTransition) -> set[str]:
    tokens: set[str] = set()
    if t.action:
        tokens.add(t.action)
    when = t.when
    if isinstance(when, str):
        tokens.add(when)
    elif isinstance(when, list):
        tokens.update(when)
    return tokens


class TransitionEvaluator:
    """A pure evaluator over a :class:`WorkflowDefinition` (no IO, no clock)."""

    def __init__(self, definition: WorkflowDefinition) -> None:
        self._definition = definition

    @property
    def definition(self) -> WorkflowDefinition:
        return self._definition

    def _outgoing(self, state: str) -> list[WorkflowTransition]:
        return [t for t in self._definition.transitions if t.from_state == state]

    def allowed_events(self, state: str | WorkflowState) -> list[str]:
        """Every event token accepted from ``state`` (definition order, deduped)."""
        state_value = state.value if isinstance(state, WorkflowState) else state
        seen: list[str] = []
        for t in self._outgoing(state_value):
            for token in sorted(_triggers(t)):
                if token not in seen:
                    seen.append(token)
        return seen

    def _guard_value(self, name: str, ctx: PureGuardContext, flags: dict[str, Any]) -> bool:
        if name == RETRY_BUDGET_REMAINING:
            return ctx.retry_count < ctx.max_retries
        if name == RETRY_BUDGET_EXHAUSTED:
            return ctx.retry_count >= ctx.max_retries
        return bool(flags.get(name, False))

    def resolve(
        self,
        state: str | WorkflowState,
        event: str,
        *,
        pure_guard_ctx: PureGuardContext | None = None,
    ) -> TransitionDecision:
        """Resolve ``(state, event)`` to its single enabled transition.

        Raises :class:`InvalidTransitionError` when no rule matches the event,
        :class:`PreconditionError` when a matching rule's ``preconditions`` are
        unmet, and :class:`GuardFailedError` when a matching rule's guards are
        unmet. Mirrors the FSM's resolution but distinguishes the failure modes.
        """
        ctx = pure_guard_ctx or PureGuardContext()
        flags = ctx.as_flags()
        state_value = state.value if isinstance(state, WorkflowState) else state

        candidates = [t for t in self._outgoing(state_value) if event in _triggers(t)]
        if not candidates:
            raise InvalidTransitionError(state_value, event)

        guard_failures: list[str] = []
        for t in candidates:
            unmet_pre = self._unmet_preconditions(t, ctx)
            if unmet_pre:
                # A precondition miss is a distinct, listable failure.
                raise PreconditionError(state_value, event, unmet_pre)

            results, unmet = self._evaluate_guards(t, event, ctx, flags)
            if not unmet:
                return TransitionDecision(
                    to_state=WorkflowState(t.to_state),
                    guard_results=results,
                    effects=[t.action] if t.action else [],
                    record=t.record,
                    skill=t.skill,
                )
            guard_failures.extend(unmet)

        raise GuardFailedError(state_value, event, sorted(set(guard_failures)))

    def _unmet_preconditions(self, t: WorkflowTransition, ctx: PureGuardContext) -> list[str]:
        if not t.preconditions:
            return []
        loaded = ctx.preconditions or {}
        return [p for p in t.preconditions if not loaded.get(p, False)]

    def _evaluate_guards(
        self,
        t: WorkflowTransition,
        event: str,
        ctx: PureGuardContext,
        flags: dict[str, Any],
    ) -> tuple[dict[str, bool], list[str]]:
        results: dict[str, bool] = {}
        unmet: list[str] = []

        if t.condition:
            value = self._guard_value(t.condition, ctx, flags)
            results[t.condition] = value
            if not value:
                unmet.append(t.condition)

        if isinstance(t.when, list):
            for signal in t.when:
                if signal == event:
                    results[signal] = True
                    continue
                value = self._guard_value(signal, ctx, flags)
                results[signal] = value
                if not value:
                    unmet.append(signal)
        elif isinstance(t.when, str) and t.when != event:
            # A scalar ``when`` that is not the triggering event is treated as a
            # flag guard (parity with the FSM).
            value = self._guard_value(t.when, ctx, flags)
            results[t.when] = value
            if not value:
                unmet.append(t.when)

        return results, unmet


__all__ = [
    "PureGuardContext",
    "TransitionDecision",
    "TransitionEvaluator",
]
