"""Tests for the deterministic postmortem composer (F17, AC13)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from forge_board.incidents import TemplatePostmortemComposer, content_hash, render_postmortem_md
from forge_contracts.enums import IncidentSeverity, IncidentState
from forge_contracts.incident import (
    BlastRadius,
    IncidentEventDTO,
    IncidentSnapshot,
    Runbook,
    RunbookStep,
)


def _snapshot() -> IncidentSnapshot:
    return IncidentSnapshot(
        id=uuid.uuid4(),
        key="CORE-INC1",
        project_id=uuid.uuid4(),
        title="Checkout latency spike",
        severity=IncidentSeverity.HIGH,
        state=IncidentState.RESOLVED,
    )


def _events(incident_id: uuid.UUID) -> list[IncidentEventDTO]:
    base = datetime(2026, 6, 27, 10, 0, tzinfo=UTC)
    return [
        IncidentEventDTO(
            incident_id=incident_id,
            sequence=1,
            kind="state_change",
            summary="alert_received -> incident_created",
            created_at=base,
        ),
        IncidentEventDTO(
            incident_id=incident_id,
            sequence=2,
            kind="context_finding",
            summary="Error rate 12% on checkout-api",
            created_at=base,
        ),
        IncidentEventDTO(
            incident_id=incident_id,
            sequence=3,
            kind="impact",
            summary="Degraded checkout for ~8% of users",
            data={"affected_services": ["checkout-api"]},
            created_at=base,
        ),
        IncidentEventDTO(
            incident_id=incident_id,
            sequence=4,
            kind="runbook_step",
            summary="restart checkout-api pods",
            created_at=base,
        ),
    ]


def test_compose_produces_timeline_root_cause_and_action_items() -> None:
    snap = _snapshot()
    composer = TemplatePostmortemComposer()
    pm = composer.compose(incident=snap, events=_events(snap.id), plans=[])
    assert pm.timeline, "expected a non-empty timeline"
    assert pm.root_cause
    assert len(pm.action_items) >= 1
    assert pm.incident_id == snap.id


def test_compose_is_deterministic() -> None:
    snap = _snapshot()
    composer = TemplatePostmortemComposer()
    events = _events(snap.id)
    md1 = render_postmortem_md(composer.compose(incident=snap, events=events, plans=[]))
    md2 = render_postmortem_md(composer.compose(incident=snap, events=events, plans=[]))
    assert content_hash(md1) == content_hash(md2)


def test_high_blast_plan_step_yields_durability_action_item() -> None:
    snap = _snapshot()
    plan = Runbook(
        incident_id=snap.id,
        steps=[
            RunbookStep(
                id="s1",
                order=1,
                title="failover db",
                action="restart_service",
                blast_radius=BlastRadius.MEDIUM,
            )
        ],
    )
    pm = TemplatePostmortemComposer().compose(incident=snap, events=_events(snap.id), plans=[plan])
    titles = " ".join(item.title for item in pm.action_items)
    assert "Make remediation durable" in titles
