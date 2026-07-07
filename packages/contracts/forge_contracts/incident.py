"""Incident-workflow DTOs and Protocols (F17 — additive to the frozen surface).

This module is *additive*: it does not modify the frozen ``forge_contracts``
public surface in ``dtos.py``/``__init__.py``. It supplies the transport models
and structural Protocols the incident slice (alert ingest → diagnosis →
remediation → recovery → postmortem) is built on.

Foundation conformance notes (deviations from the idealized slice spec):

* ``IncidentSeverity`` is reused from :mod:`forge_contracts.enums`
  (``low|medium|high|critical``); the slice's ``sev1..sev4`` vocabulary is not
  the foundation's, so provider adapters map onto the foundation enum instead.
* ``BlastRadius`` is defined here (the spec sources it from a never-built
  ``forge_contracts.skill``); it is the ordered ``low < medium < high`` scale the
  ``incident-response`` skill profile caps at ``low``.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from forge_contracts.enums import IncidentSeverity, IncidentState


class _Model(BaseModel):
    """Shared base: tolerant of unknown keys, populatable by field name or alias."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class BlastRadius(enum.StrEnum):
    """Ordered impact scale of a remediation action (low < medium < high)."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AlertProvider(enum.StrEnum):
    """The source of an inbound incident alert."""

    PAGERDUTY = "pagerduty"
    DATADOG = "datadog"
    SENTRY = "sentry"
    GRAFANA = "grafana"
    MANUAL = "manual"
    WEBHOOK = "webhook"


#: Ordering used by blast-radius comparisons (defense-in-depth caps).
BLAST_ORDER: dict[str, int] = {
    BlastRadius.LOW.value: 0,
    BlastRadius.MEDIUM.value: 1,
    BlastRadius.HIGH.value: 2,
}


def blast_rank(value: str | BlastRadius) -> int:
    """Rank a blast-radius value; unknown values sort *above* high (most severe)."""
    return BLAST_ORDER.get(str(value), len(BLAST_ORDER))


# --------------------------------------------------------------------------- #
# Alert intake                                                                 #
# --------------------------------------------------------------------------- #


class IncidentAlert(_Model):
    """A normalized inbound alert (provider webhook or manual declaration).

    The raw provider payload is intentionally *not* carried beyond normalization
    (secret-redaction rule); only a derived dedup key and the redacted summary
    fields survive.
    """

    provider: AlertProvider
    external_id: str | None = None
    delivery_id: str | None = None
    dedup_key: str
    title: str
    severity: IncidentSeverity = IncidentSeverity.MEDIUM
    service: str | None = None
    repo_id: str | None = None
    description: str | None = None
    received_at: datetime | None = None


# --------------------------------------------------------------------------- #
# Diagnosis / impact                                                           #
# --------------------------------------------------------------------------- #


class ContextFinding(_Model):
    """A single read-only diagnostic finding with provenance."""

    kind: Literal["log", "metric", "repo", "knowledge", "mcp", "diagnostic"]
    summary: str
    source: str
    refs: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)


class ImpactAssessment(_Model):
    """The agent/human assessment of an incident's blast radius and impact."""

    blast_radius: BlastRadius = BlastRadius.LOW
    affected_services: list[str] = Field(default_factory=list)
    user_impact: str = ""
    severity_recommendation: IncidentSeverity = IncidentSeverity.MEDIUM


# --------------------------------------------------------------------------- #
# Remediation runbook                                                          #
# --------------------------------------------------------------------------- #


class RunbookStep(_Model):
    """One ordered remediation step with a declared blast radius."""

    id: str
    order: int = 0
    title: str
    action: str
    args: dict[str, Any] = Field(default_factory=dict)
    blast_radius: BlastRadius = BlastRadius.LOW
    rationale: str = ""
    status: Literal["proposed", "approved", "skipped", "running", "succeeded", "failed"] = (
        "proposed"
    )


class Runbook(_Model):
    """An ordered, proposed remediation plan (read-only until approved)."""

    incident_id: uuid.UUID
    attempt: int = 1
    steps: list[RunbookStep] = Field(default_factory=list)
    max_blast_radius: BlastRadius = BlastRadius.LOW
    proposed_by_agent_run_id: uuid.UUID | None = None

    def rollup_blast_radius(self) -> BlastRadius:
        """The maximum blast radius across all steps (low when empty)."""
        if not self.steps:
            return BlastRadius.LOW
        worst = max(self.steps, key=lambda s: blast_rank(s.blast_radius))
        return BlastRadius(worst.blast_radius)


class StepResult(_Model):
    """The outcome of executing a single runbook step."""

    step_id: str
    status: Literal["succeeded", "failed", "skipped"]
    summary: str = ""
    output_ref: str | None = None


# --------------------------------------------------------------------------- #
# Recovery                                                                     #
# --------------------------------------------------------------------------- #


class RecoveryStatus(_Model):
    """A recovery-monitor reading of the incident's health signals."""

    recovered: bool = False
    healthy_signals: list[str] = Field(default_factory=list)
    degraded_signals: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Postmortem + follow-up work                                                  #
# --------------------------------------------------------------------------- #


class ActionItem(_Model):
    """A follow-up action item that becomes a board Task."""

    title: str
    description: str = ""
    kind: Literal["bug", "chore"] = "chore"
    priority: str = "medium"
    owner_hint: str | None = None


class PostmortemTimelineEntry(_Model):
    """One entry on the postmortem timeline."""

    at: datetime
    summary: str


class Postmortem(_Model):
    """A structured postmortem composed from the incident timeline."""

    incident_id: uuid.UUID
    summary: str = ""
    timeline: list[PostmortemTimelineEntry] = Field(default_factory=list)
    root_cause: str = ""
    contributing_factors: list[str] = Field(default_factory=list)
    resolution: str = ""
    lessons_learned: list[str] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Read-side snapshots (inputs to the composer / API)                          #
# --------------------------------------------------------------------------- #


class IncidentEventDTO(_Model):
    """An append-only incident timeline event (state change / finding / step)."""

    id: uuid.UUID | None = None
    incident_id: uuid.UUID | None = None
    workflow_run_id: uuid.UUID | None = None
    sequence: int = 0
    kind: str = "note"
    actor: str = "system"
    summary: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class IncidentSnapshot(_Model):
    """A read-only view of an incident used by the postmortem composer."""

    id: uuid.UUID
    key: str | None = None
    project_id: uuid.UUID | None = None
    title: str = ""
    description: str | None = None
    severity: IncidentSeverity = IncidentSeverity.MEDIUM
    state: IncidentState = IncidentState.ALERT_RECEIVED
    blast_radius: str | None = None
    impact_summary: str | None = None
    detected_at: datetime | None = None
    resolved_at: datetime | None = None


# --------------------------------------------------------------------------- #
# Structural Protocols (implemented by board-core / F06 wiring; doubles in tests)
# --------------------------------------------------------------------------- #


@runtime_checkable
class IncidentAgentPort(Protocol):
    """Backed by the agent runtime with ``skill_profile='incident-response'``."""

    async def gather_context(
        self, *, incident_id: uuid.UUID, repo_id: str | None, knowledge_scope: dict[str, Any]
    ) -> list[ContextFinding]: ...

    async def assess_impact(
        self, *, incident_id: uuid.UUID, findings: list[ContextFinding]
    ) -> ImpactAssessment: ...

    async def propose_remediation(
        self,
        *,
        incident_id: uuid.UUID,
        attempt: int,
        assessment: ImpactAssessment,
        findings: list[ContextFinding],
    ) -> Runbook: ...


@runtime_checkable
class RunbookExecutor(Protocol):
    """Executes a single approved runbook step (re-checked at execution time)."""

    async def execute_step(
        self,
        step: RunbookStep,
        *,
        incident_id: uuid.UUID,
        directives: Any,
        policy: Any,
    ) -> StepResult: ...


@runtime_checkable
class RecoveryMonitor(Protocol):
    """Reads recovery signals to decide whether an incident has recovered."""

    async def check_recovery(
        self, *, incident_id: uuid.UUID, assessment: ImpactAssessment
    ) -> RecoveryStatus: ...


@runtime_checkable
class PostmortemComposer(Protocol):
    """Composes a :class:`Postmortem` from the incident timeline (no LLM in V1)."""

    def compose(
        self,
        *,
        incident: IncidentSnapshot,
        events: list[IncidentEventDTO],
        plans: list[Runbook],
    ) -> Postmortem: ...


@runtime_checkable
class AlertAdapter(Protocol):
    """Maps a provider webhook to a normalized :class:`IncidentAlert`."""

    provider: AlertProvider

    def verify(self, *, secret: str, body: bytes, headers: dict[str, str]) -> bool: ...

    def normalize(self, *, body: bytes, headers: dict[str, str]) -> IncidentAlert: ...


__all__ = [
    "BLAST_ORDER",
    "ActionItem",
    "AlertAdapter",
    "AlertProvider",
    "BlastRadius",
    "ContextFinding",
    "ImpactAssessment",
    "IncidentAgentPort",
    "IncidentAlert",
    "IncidentEventDTO",
    "IncidentSnapshot",
    "Postmortem",
    "PostmortemComposer",
    "PostmortemTimelineEntry",
    "RecoveryMonitor",
    "RecoveryStatus",
    "Runbook",
    "RunbookExecutor",
    "RunbookStep",
    "StepResult",
    "blast_rank",
]
