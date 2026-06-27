"""Incident REST request/response schemas (F17)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from forge_contracts.enums import IncidentSeverity, IncidentState
from forge_contracts.incident import AlertProvider, BlastRadius, RunbookStep


class IncidentDeclareRequest(BaseModel):
    """Body for ``POST /incidents`` (manual declaration)."""

    project_id: uuid.UUID
    title: str
    severity: IncidentSeverity = IncidentSeverity.MEDIUM
    description: str | None = None
    repo_id: str | None = None
    commander_id: uuid.UUID | None = None


class ManualAlertRequest(BaseModel):
    """Body for ``POST /integrations/alerts/manual``."""

    project_id: uuid.UUID
    title: str
    dedup_key: str | None = None
    severity: IncidentSeverity = IncidentSeverity.MEDIUM
    service: str | None = None
    description: str | None = None


class IncidentEventRequest(BaseModel):
    """Body for ``POST /incidents/{id}/events`` — drive the incident FSM."""

    event: str
    context: dict[str, bool] = Field(default_factory=dict)
    note: str | None = None


class ProposeRemediationRequest(BaseModel):
    """Body for ``POST /incidents/{id}/remediation`` — propose a runbook."""

    steps: list[RunbookStep]


class IncidentView(BaseModel):
    """An incident summary."""

    id: uuid.UUID
    key: str
    project_id: uuid.UUID
    title: str
    description: str | None = None
    severity: IncidentSeverity
    state: IncidentState
    lifecycle_state: str
    source: str
    dedup_key: str | None = None
    commander_id: uuid.UUID | None = None
    blast_radius: str | None = None
    impact_summary: str | None = None
    created_at: datetime
    detected_at: datetime | None = None
    acknowledged_at: datetime | None = None
    resolved_at: datetime | None = None
    allowed_events: list[str] = Field(default_factory=list)


class IncidentEventView(BaseModel):
    """One incident timeline event."""

    id: uuid.UUID
    incident_id: uuid.UUID
    sequence: int
    kind: str
    actor: str
    summary: str
    data: dict = Field(default_factory=dict)
    created_at: datetime


class RemediationStepView(BaseModel):
    id: str
    order: int
    title: str
    action: str
    blast_radius: BlastRadius
    rationale: str = ""
    status: str = "proposed"
    blocked: bool = False


class RemediationPlanView(BaseModel):
    id: uuid.UUID
    incident_id: uuid.UUID
    attempt: int
    max_blast_radius: BlastRadius
    status: str
    steps: list[RemediationStepView]
    offending_step_ids: list[str] = Field(default_factory=list)


class IncidentDetailView(IncidentView):
    """Incident detail: summary + latest plan + recent timeline."""

    remediation_plan: RemediationPlanView | None = None
    event_count: int = 0


class PostmortemView(BaseModel):
    id: uuid.UUID
    incident_id: uuid.UUID
    status: str
    content_md: str
    root_cause: str | None = None
    action_item_task_keys: list[str] = Field(default_factory=list)


class AlertAccepted(BaseModel):
    """Response for an accepted alert webhook."""

    status: str
    incident_id: uuid.UUID | None = None
    incident_key: str | None = None


__all__ = [
    "AlertAccepted",
    "AlertProvider",
    "IncidentDeclareRequest",
    "IncidentDetailView",
    "IncidentEventRequest",
    "IncidentEventView",
    "IncidentView",
    "ManualAlertRequest",
    "PostmortemView",
    "ProposeRemediationRequest",
    "RemediationPlanView",
    "RemediationStepView",
]
