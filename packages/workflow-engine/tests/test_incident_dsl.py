"""Incident workflow definition + drift tests (F17)."""

from __future__ import annotations

from pathlib import Path

import pytest

from forge_workflow import (
    InvalidTransitionError,
    default_incident_definition,
    drive_incident,
    incident_graph,
    load_definition,
)
from forge_workflow.incident.definition import INCIDENT_STATES

REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = REPO_ROOT / "examples" / "workflows" / "incident.yaml"


def test_loads_incident_definition() -> None:
    definition = default_incident_definition()
    assert definition.name == "incident"
    graph = incident_graph()
    # All 10 forward states are reachable as transition endpoints.
    forward = {
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
    }
    assert forward <= graph.states <= INCIDENT_STATES


def test_bundled_and_example_incident_dsl_match() -> None:
    bundled = default_incident_definition()
    example = load_definition(EXAMPLE)
    assert example == bundled


def test_full_happy_path() -> None:
    """Drive alert_received -> closed with passing guards (AC5)."""
    context = {
        "remediation_within_blast_radius": True,
        "approval_granted": True,
        "postmortem_persisted": True,
    }
    path = [
        ("alert_received", "alert_ingested", "incident_created"),
        ("incident_created", "incident_acknowledged", "context_gathering"),
        ("context_gathering", "context_gathered", "impact_assessed"),
        ("impact_assessed", "impact_assessed", "remediation_proposed"),
        ("remediation_proposed", "remediation_proposed", "awaiting_approval"),
        ("awaiting_approval", "remediation_approved", "executing_runbook"),
        ("executing_runbook", "runbook_completed", "monitoring"),
        ("monitoring", "recovery_confirmed", "resolved"),
        ("resolved", "postmortem_requested", "postmortem_created"),
        ("postmortem_created", "close", "closed"),
    ]
    for state, event, expected in path:
        outcome = drive_incident(state, event, context=context)
        assert outcome.to_state == expected


def test_blast_radius_gate_exceeded() -> None:
    outcome = drive_incident(
        "remediation_proposed",
        "remediation_blast_radius_exceeded",
        context={"remediation_exceeds_blast_radius": True},
    )
    assert outcome.to_state == "needs_human_input"


def test_approval_required_before_execution() -> None:
    # No approval flag -> the approve edge does not fire (no execution).
    with pytest.raises(InvalidTransitionError):
        drive_incident("awaiting_approval", "remediation_approved", context={})
    # With approval -> proceeds.
    outcome = drive_incident(
        "awaiting_approval", "remediation_approved", context={"approval_granted": True}
    )
    assert outcome.to_state == "executing_runbook"


def test_postmortem_persisted_guards_close() -> None:
    with pytest.raises(InvalidTransitionError):
        drive_incident("postmortem_created", "close", context={})
    outcome = drive_incident(
        "postmortem_created", "close", context={"postmortem_persisted": True}
    )
    assert outcome.to_state == "closed"


def test_recovery_retry_then_escalate() -> None:
    # Budget remaining (count 0 < max 2): re-propose, retry consumed.
    o1 = drive_incident(
        "monitoring", "recovery_failed", retry_count=0, max_retries=2
    )
    assert o1.to_state == "remediation_proposed"
    assert o1.retry_consumed is True
    assert o1.retry_count == 1
    # Budget exhausted (count 2 >= max 2): escalate.
    o2 = drive_incident(
        "monitoring", "recovery_failed", retry_count=2, max_retries=2
    )
    assert o2.to_state == "needs_human_input"


def test_runbook_step_failure_retry_paths() -> None:
    o1 = drive_incident("executing_runbook", "runbook_step_failed", retry_count=0, max_retries=2)
    assert o1.to_state == "remediation_proposed"
    o2 = drive_incident("executing_runbook", "runbook_step_failed", retry_count=2, max_retries=2)
    assert o2.to_state == "needs_human_input"


def test_universal_cancel_fail_resume() -> None:
    assert drive_incident("monitoring", "cancel").to_state == "cancelled"
    assert drive_incident("context_gathering", "fail").to_state == "failed"
    resumed = drive_incident(
        "needs_human_input", "resume", context={"paused_from": "remediation_proposed"}
    )
    assert resumed.to_state == "remediation_proposed"


def test_invalid_event_raises() -> None:
    with pytest.raises(InvalidTransitionError):
        drive_incident("alert_received", "recovery_confirmed")
