"""Tests for the additive incident contracts module (F17)."""

from __future__ import annotations

import uuid

from forge_contracts.enums import IncidentSeverity
from forge_contracts.incident import (
    AlertProvider,
    BlastRadius,
    IncidentAlert,
    Runbook,
    RunbookStep,
    blast_rank,
)


def test_blast_rank_ordering() -> None:
    assert blast_rank(BlastRadius.LOW) < blast_rank(BlastRadius.MEDIUM)
    assert blast_rank(BlastRadius.MEDIUM) < blast_rank(BlastRadius.HIGH)
    # Unknown sorts above high (most severe / fail-safe).
    assert blast_rank("unknown") > blast_rank(BlastRadius.HIGH)


def test_incident_alert_defaults() -> None:
    alert = IncidentAlert(provider=AlertProvider.DATADOG, dedup_key="svc-x:cpu", title="High CPU")
    assert alert.severity is IncidentSeverity.MEDIUM
    assert alert.provider is AlertProvider.DATADOG


def test_runbook_rollup_blast_radius() -> None:
    inc = uuid.uuid4()
    rb = Runbook(
        incident_id=inc,
        steps=[
            RunbookStep(id="s1", order=1, title="read logs", action="read_logs"),
            RunbookStep(
                id="s2",
                order=2,
                title="restart",
                action="restart_service",
                blast_radius=BlastRadius.MEDIUM,
            ),
        ],
    )
    assert rb.rollup_blast_radius() is BlastRadius.MEDIUM


def test_runbook_rollup_empty_is_low() -> None:
    assert Runbook(incident_id=uuid.uuid4()).rollup_blast_radius() is BlastRadius.LOW
