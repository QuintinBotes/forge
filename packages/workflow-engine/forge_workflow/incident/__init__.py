"""Incident workflow definition, state/event vocabulary, guards, and FSM driver.

F17 extends the foundation FSM (DSL parser + :class:`TransitionGraph`) with the
``incident`` workflow definition and a state-agnostic driver. The incident
lifecycle is expressed over the :class:`forge_contracts.IncidentState` vocabulary
(plus the shared error states), driven by the same ``TransitionGraph`` the
feature workflow uses.
"""

from __future__ import annotations

from forge_workflow.incident.definition import (
    INCIDENT_DEFINITION_NAME,
    INCIDENT_EVENTS,
    INCIDENT_GUARDS,
    INCIDENT_STATES,
    INCIDENT_TERMINAL_STATES,
    default_incident_definition,
    incident_graph,
)
from forge_workflow.incident.fsm import (
    IncidentTransitionOutcome,
    allowed_incident_events,
    drive_incident,
)

__all__ = [
    "INCIDENT_DEFINITION_NAME",
    "INCIDENT_EVENTS",
    "INCIDENT_GUARDS",
    "INCIDENT_STATES",
    "INCIDENT_TERMINAL_STATES",
    "IncidentTransitionOutcome",
    "allowed_incident_events",
    "default_incident_definition",
    "drive_incident",
    "incident_graph",
]
