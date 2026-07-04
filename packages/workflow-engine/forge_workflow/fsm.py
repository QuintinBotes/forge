"""The validated FSM transition graph and guard evaluation.

This is the pure, storage-free core of the engine. A :class:`TransitionGraph`
wraps a :class:`~forge_contracts.WorkflowDefinition`, validates its structure,
and resolves a ``(state, event)`` pair to the single enabled transition.

Guard semantics
---------------
The ``event`` passed to :meth:`TransitionGraph.find` is the trigger that just
occurred. A transition is a *candidate* when the event matches its ``action`` or
appears in its ``when`` clause. A candidate *fires* when:

* its ``condition`` guard (if any) evaluates true, and
* for a list-valued ``when`` (an AND of signals), every listed signal is
  satisfied — either it equals the triggering event, is truthy in ``context``,
  or is a built-in guard that evaluates true.

Two built-in guards drive retry/escalation (spec ``retry_policy``):
``retry_budget_remaining`` (``retry_count < max_retries``) and
``retry_budget_exhausted`` (``retry_count >= max_retries``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from forge_contracts import WorkflowDefinition, WorkflowTransition
from forge_workflow.exceptions import (
    AmbiguousTransitionError,
    InvalidTransitionError,
    WorkflowDefinitionError,
)

#: Built-in guard name: the retry budget still has room.
RETRY_BUDGET_REMAINING = "retry_budget_remaining"
#: Built-in guard name: the retry budget is spent.
RETRY_BUDGET_EXHAUSTED = "retry_budget_exhausted"


def evaluate_guard(
    name: str,
    *,
    context: dict[str, Any],
    retry_count: int,
    max_retries: int,
) -> bool:
    """Evaluate a single guard ``name`` against the run context + retry budget.

    Built-in retry guards are computed from the budget; any other name is read
    as a boolean flag from ``context`` (absent -> ``False``).
    """
    if name == RETRY_BUDGET_REMAINING:
        return retry_count < max_retries
    if name == RETRY_BUDGET_EXHAUSTED:
        return retry_count >= max_retries
    return bool(context.get(name, False))


def _triggers(transition: WorkflowTransition) -> set[str]:
    """Every event token that can trigger ``transition`` (action + when)."""
    tokens: set[str] = set()
    if transition.action:
        tokens.add(transition.action)
    when = transition.when
    if isinstance(when, str):
        tokens.add(when)
    elif isinstance(when, list):
        tokens.update(when)
    return tokens


@dataclass
class TransitionGraph:
    """A workflow definition viewed as a directed transition graph."""

    definition: WorkflowDefinition

    @classmethod
    def from_definition(cls, definition: WorkflowDefinition) -> TransitionGraph:
        """Build and validate a graph; raise ``WorkflowDefinitionError`` if invalid."""
        graph = cls(definition=definition)
        graph.validate()
        return graph

    # -- structure ------------------------------------------------------------ #

    @property
    def states(self) -> set[str]:
        """Every state referenced as a transition source or target."""
        out: set[str] = set()
        for t in self.definition.transitions:
            out.add(t.from_state)
            out.add(t.to_state)
        return out

    @property
    def initial_state(self) -> str:
        """The starting state (``created`` if present, else the first source)."""
        if "created" in self.states:
            return "created"
        return self.definition.transitions[0].from_state

    def transitions_from(self, state: str) -> list[WorkflowTransition]:
        """Outgoing transitions from ``state`` (definition order preserved)."""
        return [t for t in self.definition.transitions if t.from_state == state]

    # -- validation ----------------------------------------------------------- #

    def validate(self) -> None:
        """Raise :class:`WorkflowDefinitionError` if the graph is malformed."""
        transitions = self.definition.transitions
        if not transitions:
            raise WorkflowDefinitionError("workflow has no transitions")

        seen: set[tuple[str, frozenset[str], str | None]] = set()
        for t in transitions:
            if not t.from_state or not t.to_state:
                raise WorkflowDefinitionError("every transition needs a non-empty 'from' and 'to'")
            # An exact duplicate (same source, same triggers, same condition) is
            # unresolvable at runtime, so reject it up front.
            key = (t.from_state, frozenset(_triggers(t)), t.condition)
            if key in seen:
                raise WorkflowDefinitionError(
                    f"duplicate transition from {t.from_state!r} on {sorted(_triggers(t))!r}"
                )
            seen.add(key)

    # -- resolution ----------------------------------------------------------- #

    def _event_matches(self, transition: WorkflowTransition, event: str) -> bool:
        return event in _triggers(transition)

    def _fires(
        self,
        transition: WorkflowTransition,
        event: str,
        context: dict[str, Any],
        retry_count: int,
        max_retries: int,
    ) -> bool:
        if transition.condition and not evaluate_guard(
            transition.condition,
            context=context,
            retry_count=retry_count,
            max_retries=max_retries,
        ):
            return False
        # A list-valued ``when`` is an AND of signals: all must hold.
        if isinstance(transition.when, list):
            for signal in transition.when:
                if signal == event:
                    continue
                if not evaluate_guard(
                    signal,
                    context=context,
                    retry_count=retry_count,
                    max_retries=max_retries,
                ):
                    return False
        return True

    def find(
        self,
        state: str,
        event: str,
        *,
        context: dict[str, Any] | None = None,
        retry_count: int = 0,
        max_retries: int = 0,
    ) -> WorkflowTransition:
        """Resolve ``(state, event)`` to the one enabled transition.

        Raises :class:`InvalidTransitionError` if none is enabled and
        :class:`AmbiguousTransitionError` if more than one is.
        """
        ctx = context or {}
        candidates = [t for t in self.transitions_from(state) if self._event_matches(t, event)]
        eligible = [t for t in candidates if self._fires(t, event, ctx, retry_count, max_retries)]
        if not eligible:
            raise InvalidTransitionError(state, event)
        if len(eligible) > 1:
            raise AmbiguousTransitionError(state, event, [t.to_state for t in eligible])
        return eligible[0]


__all__ = [
    "RETRY_BUDGET_EXHAUSTED",
    "RETRY_BUDGET_REMAINING",
    "TransitionGraph",
    "evaluate_guard",
]
