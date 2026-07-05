"""Load and validate the bundled ``incident`` workflow definition."""

from __future__ import annotations

from functools import lru_cache
from importlib import resources

from forge_contracts import WorkflowDefinition

INCIDENT_DEFINITION_NAME = "incident"

#: The 10 forward incident states + the shared error/terminal states.
INCIDENT_STATES: frozenset[str] = frozenset(
    {
        "alert_received",
        "incident_created",
        "context_gathering",
        "impact_assessed",
        "remediation_proposed",
        "awaiting_approval",
        "executing_runbook",
        "monitoring",
        "resolved",
        "postmortem_created",
        "closed",
        "needs_human_input",
        "failed",
        "cancelled",
    }
)

#: Incident FSM trigger events (the ``action``/``when`` tokens in the DSL).
INCIDENT_EVENTS: frozenset[str] = frozenset(
    {
        "alert_ingested",
        "incident_acknowledged",
        "context_gathered",
        "impact_assessed",
        "remediation_proposed",
        "remediation_blast_radius_exceeded",
        "remediation_approved",
        "remediation_rejected",
        "runbook_completed",
        "runbook_step_failed",
        "recovery_confirmed",
        "recovery_failed",
        "postmortem_requested",
        # universal edges applied by the incident service:
        "close",
        "resume",
        "cancel",
        "fail",
    }
)

#: Incident-specific guard names (context flags) the domain layer sets.
INCIDENT_GUARDS: frozenset[str] = frozenset(
    {
        "remediation_within_blast_radius",
        "remediation_exceeds_blast_radius",
        "approval_granted",
        "postmortem_persisted",
        # reused engine built-ins for the recovery retry loop:
        "retry_budget_remaining",
        "retry_budget_exhausted",
    }
)

#: States from which no forward transition is taken (run is finished).
INCIDENT_TERMINAL_STATES: frozenset[str] = frozenset({"closed", "failed", "cancelled"})


@lru_cache(maxsize=1)
def default_incident_definition() -> WorkflowDefinition:
    """Return the parsed, validated bundled ``incident`` :class:`WorkflowDefinition`."""
    from forge_workflow.dsl import parse_definition

    text = (
        resources.files("forge_workflow")
        .joinpath("definitions", "incident.yaml")
        .read_text(encoding="utf-8")
    )
    return parse_definition(text)


@lru_cache(maxsize=1)
def incident_graph():
    """Return the validated :class:`TransitionGraph` for the incident workflow."""
    from forge_workflow.fsm import TransitionGraph

    return TransitionGraph.from_definition(default_incident_definition())


__all__ = [
    "INCIDENT_DEFINITION_NAME",
    "INCIDENT_EVENTS",
    "INCIDENT_GUARDS",
    "INCIDENT_STATES",
    "INCIDENT_TERMINAL_STATES",
    "default_incident_definition",
    "incident_graph",
]
