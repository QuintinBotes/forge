"""State-agnostic driver for the incident FSM.

The foundation :class:`~forge_workflow.engine.WorkflowEngineImpl` hard-codes a
``WorkflowState(...)`` return (feature states only), so it cannot drive incident
runs. This module reuses the pure :class:`~forge_workflow.fsm.TransitionGraph`
core to resolve incident transitions over string states, applies the same
retry-budget accounting the feature engine uses, and adds the universal
``cancel``/``fail``/``resume`` edges (which the foundation graph does not inject).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from forge_contracts import WorkflowTransition
from forge_workflow.exceptions import InvalidTransitionError
from forge_workflow.fsm import RETRY_BUDGET_REMAINING, TransitionGraph
from forge_workflow.incident.definition import (
    INCIDENT_TERMINAL_STATES,
    incident_graph,
)

#: Context key holding the state a run was paused from (for ``resume``).
PAUSED_FROM_KEY = "paused_from"

_PAUSED_STATE = "needs_human_input"


@dataclass(frozen=True)
class IncidentTransitionOutcome:
    """The result of driving one incident event."""

    to_state: str
    retry_count: int
    transition: WorkflowTransition | None = None
    retry_consumed: bool = False


def _universal_target(state: str, event: str, context: dict[str, Any]) -> str | None:
    """Resolve a universal (non-graph) edge, or ``None`` if not applicable."""
    if state in INCIDENT_TERMINAL_STATES:
        return None
    if event == "cancel":
        return "cancelled"
    if event == "fail":
        return "failed"
    if event == "resume" and state == _PAUSED_STATE:
        target = context.get(PAUSED_FROM_KEY)
        return str(target) if target else "remediation_proposed"
    return None


def drive_incident(
    state: str,
    event: str,
    *,
    context: dict[str, Any] | None = None,
    retry_count: int = 0,
    max_retries: int = 2,
    graph: TransitionGraph | None = None,
) -> IncidentTransitionOutcome:
    """Resolve ``(state, event)`` to the next incident state.

    Forward edges are resolved by the validated transition graph; ``cancel`` /
    ``fail`` / ``resume`` are handled as universal edges. Taking a
    ``retry_budget_remaining`` edge consumes one retry (mirroring the feature
    engine). Raises :class:`InvalidTransitionError` when no edge fires.
    """
    ctx = dict(context or {})
    universal = _universal_target(state, event, ctx)
    if universal is not None:
        return IncidentTransitionOutcome(to_state=universal, retry_count=retry_count)

    g = graph or incident_graph()
    transition = g.find(
        state,
        event,
        context=ctx,
        retry_count=retry_count,
        max_retries=max_retries,
    )
    consumed = transition.condition == RETRY_BUDGET_REMAINING
    new_retry = retry_count + 1 if consumed else retry_count
    return IncidentTransitionOutcome(
        to_state=transition.to_state,
        retry_count=new_retry,
        transition=transition,
        retry_consumed=consumed,
    )


def allowed_incident_events(
    state: str,
    *,
    context: dict[str, Any] | None = None,
    retry_count: int = 0,
    max_retries: int = 2,
    graph: TransitionGraph | None = None,
) -> list[str]:
    """Return the events that would currently fire from ``state`` (best-effort)."""
    ctx = dict(context or {})
    g = graph or incident_graph()
    events: list[str] = []
    for transition in g.transitions_from(state):
        triggers = set()
        if transition.action:
            triggers.add(transition.action)
        if isinstance(transition.when, str):
            triggers.add(transition.when)
        elif isinstance(transition.when, list):
            triggers.update(transition.when)
        for trigger in sorted(triggers):
            try:
                g.find(
                    state,
                    trigger,
                    context=ctx,
                    retry_count=retry_count,
                    max_retries=max_retries,
                )
            except InvalidTransitionError:
                continue
            events.append(trigger)
    if state not in INCIDENT_TERMINAL_STATES:
        events.extend(["cancel", "fail"])
        if state == _PAUSED_STATE:
            events.append("resume")
    return sorted(set(events))


__all__ = [
    "PAUSED_FROM_KEY",
    "IncidentTransitionOutcome",
    "allowed_incident_events",
    "drive_incident",
]
