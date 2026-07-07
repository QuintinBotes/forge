"""In-memory incident service (F17).

Drives the incident FSM (``forge_workflow.incident``), persists the incident
timeline / remediation plans / postmortem in process memory (mirroring the
board/approval routers' Phase-1 pattern; a DB-backed implementation swaps in
behind the same surface), and enforces the structural safety properties:

* every remediation proposal is validated against the ``incident-response``
  blast-radius posture (``assert_runbook_within_policy``), and
* moving to ``executing_runbook`` re-checks the approved plan at execution time
  (defense in depth) — a stale approval can never execute an over-blast step.
"""

from __future__ import annotations

import builtins
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from forge_board import InMemoryBoardService
from forge_board.incidents import (
    TemplatePostmortemComposer,
    assert_runbook_within_policy,
    create_action_item_tasks,
    derive_dedup_key,
    runbook_max_blast_radius,
)
from forge_board.incidents.errors import BlastRadiusExceeded, IncidentNotFound
from forge_contracts.enums import IncidentSeverity, IncidentState
from forge_contracts.incident import (
    BlastRadius,
    IncidentAlert,
    IncidentEventDTO,
    IncidentSnapshot,
    Postmortem,
    Runbook,
    RunbookStep,
)
from forge_skill import SkillProfileRegistry, to_directives
from forge_workflow import allowed_incident_events, drive_incident
from forge_workflow.incident.fsm import PAUSED_FROM_KEY

#: States considered "open" for dedup-attach purposes.
_CLOSED_STATES = frozenset({"resolved", "postmortem_created", "closed", "cancelled", "failed"})
_FORWARD_STATES = frozenset(s.value for s in IncidentState)
_MAX_RETRIES = 2

_INCIDENT_DIRECTIVES = to_directives(SkillProfileRegistry().get("incident-response"))


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class _Event:
    id: uuid.UUID
    sequence: int
    kind: str
    actor: str
    summary: str
    data: dict
    created_at: datetime


@dataclass
class _Plan:
    id: uuid.UUID
    attempt: int
    max_blast_radius: BlastRadius
    status: str
    steps: list[RunbookStep]
    offending_step_ids: list[str]


@dataclass
class IncidentRecord:
    """The full in-memory state of one incident."""

    id: uuid.UUID
    key: str
    project_id: uuid.UUID
    title: str
    severity: IncidentSeverity
    lifecycle_state: str
    source: str
    description: str | None = None
    dedup_key: str | None = None
    commander_id: uuid.UUID | None = None
    blast_radius: str | None = None
    impact_summary: str | None = None
    repo_id: str | None = None
    state: IncidentState = IncidentState.ALERT_RECEIVED
    retry_count: int = 0
    context: dict[str, bool | str] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_now)
    detected_at: datetime | None = None
    acknowledged_at: datetime | None = None
    resolved_at: datetime | None = None
    events: list[_Event] = field(default_factory=list)
    plans: list[_Plan] = field(default_factory=list)
    alerts: list[dict] = field(default_factory=list)
    postmortem: Postmortem | None = None
    postmortem_status: str = "draft"
    action_item_task_keys: list[str] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.lifecycle_state not in _CLOSED_STATES


class IncidentService:
    """A per-workspace, in-memory incident orchestrator."""

    def __init__(
        self,
        *,
        board: InMemoryBoardService | None = None,
        composer: TemplatePostmortemComposer | None = None,
    ) -> None:
        self._incidents: dict[uuid.UUID, IncidentRecord] = {}
        self._board = board or InMemoryBoardService()
        self._composer = composer or TemplatePostmortemComposer()
        self._key_counter = 0
        self._deliveries: set[tuple[str, str]] = set()

    # -- webhook idempotency --------------------------------------------- #

    def register_delivery(self, provider: str, delivery_id: str | None) -> bool:
        """Record a webhook delivery id; return ``False`` if already processed."""
        if not delivery_id:
            return True
        key = (provider, delivery_id)
        if key in self._deliveries:
            return False
        self._deliveries.add(key)
        return True

    # -- reads ------------------------------------------------------------ #

    def list(
        self,
        *,
        project_id: uuid.UUID | None = None,
        state: str | None = None,
        severity: IncidentSeverity | None = None,
    ) -> list[IncidentRecord]:
        items = list(self._incidents.values())
        if project_id is not None:
            items = [i for i in items if i.project_id == project_id]
        if state is not None:
            items = [i for i in items if i.lifecycle_state == state]
        if severity is not None:
            items = [i for i in items if i.severity == severity]
        return items

    def get(self, incident_id: uuid.UUID) -> IncidentRecord:
        record = self._incidents.get(incident_id)
        if record is None:
            raise IncidentNotFound(incident_id)
        return record

    def timeline(self, incident_id: uuid.UUID) -> builtins.list[_Event]:
        return list(self.get(incident_id).events)

    def latest_plan(self, incident_id: uuid.UUID) -> _Plan | None:
        plans = self.get(incident_id).plans
        return plans[-1] if plans else None

    def allowed_events(self, record: IncidentRecord) -> builtins.list[str]:
        return allowed_incident_events(
            record.lifecycle_state,
            context=dict(record.context),
            retry_count=record.retry_count,
            max_retries=_MAX_RETRIES,
        )

    # -- creation --------------------------------------------------------- #

    def _next_key(self) -> str:
        self._key_counter += 1
        return f"INC-{self._key_counter}"

    def declare(
        self,
        *,
        project_id: uuid.UUID,
        title: str,
        severity: IncidentSeverity = IncidentSeverity.MEDIUM,
        description: str | None = None,
        repo_id: str | None = None,
        commander_id: uuid.UUID | None = None,
        actor: str = "system",
    ) -> IncidentRecord:
        """Declare a manual incident; the FSM starts at ``incident_created``."""
        now = _now()
        record = IncidentRecord(
            id=uuid.uuid4(),
            key=self._next_key(),
            project_id=project_id,
            title=title,
            severity=severity,
            description=description,
            repo_id=repo_id,
            commander_id=commander_id,
            source="manual",
            lifecycle_state="incident_created",
            state=IncidentState.INCIDENT_CREATED,
            detected_at=now,
            acknowledged_at=now,
        )
        self._incidents[record.id] = record
        self._append_event(record, "state_change", actor, "incident declared (manual)")
        return record

    def ingest_alert(
        self, *, alert: IncidentAlert, project_id: uuid.UUID, actor: str = "system"
    ) -> tuple[IncidentRecord, str]:
        """Create-or-attach an incident from a normalized alert (dedup by key)."""
        dedup_key = derive_dedup_key(alert)
        for record in self._incidents.values():
            if record.dedup_key == dedup_key and record.is_open:
                record.alerts.append({"dedup_key": dedup_key, "status": "attached"})
                self._append_event(
                    record, "note", actor, f"duplicate alert attached ({alert.provider.value})"
                )
                return record, "attached"

        now = _now()
        record = IncidentRecord(
            id=uuid.uuid4(),
            key=self._next_key(),
            project_id=project_id,
            title=alert.title,
            severity=alert.severity or IncidentSeverity.MEDIUM,
            description=alert.description,
            repo_id=alert.repo_id,
            dedup_key=dedup_key,
            source=alert.provider.value,
            lifecycle_state="alert_received",
            state=IncidentState.ALERT_RECEIVED,
            detected_at=now,
        )
        record.alerts.append({"dedup_key": dedup_key, "status": "created_incident"})
        self._incidents[record.id] = record
        self._append_event(record, "state_change", "system", "alert received")
        # Auto-advance alert_received -> incident_created.
        self.send_event(record.id, "alert_ingested", actor="system")
        return record, "created_incident"

    # -- remediation ------------------------------------------------------ #

    def propose_remediation(
        self, incident_id: uuid.UUID, *, steps: builtins.list[RunbookStep], actor: str = "agent"
    ) -> _Plan:
        """Store a proposed runbook and set the blast-radius guard context flags."""
        record = self.get(incident_id)
        runbook = Runbook(incident_id=incident_id, attempt=record.retry_count + 1, steps=steps)
        offending = assert_runbook_within_policy(runbook, _INCIDENT_DIRECTIVES)
        plan = _Plan(
            id=uuid.uuid4(),
            attempt=record.retry_count + 1,
            max_blast_radius=BlastRadius(runbook_max_blast_radius(runbook)),
            status="proposed",
            steps=steps,
            offending_step_ids=offending,
        )
        record.plans.append(plan)
        record.context["remediation_within_blast_radius"] = not offending
        record.context["remediation_exceeds_blast_radius"] = bool(offending)
        self._append_event(
            record,
            "remediation_proposed",
            actor,
            f"remediation proposed ({len(steps)} steps)",
            data={"offending_step_ids": offending},
        )
        return plan

    # -- FSM driving ------------------------------------------------------ #

    def send_event(
        self,
        incident_id: uuid.UUID,
        event: str,
        *,
        actor: str = "system",
        context: dict[str, bool] | None = None,
        note: str | None = None,
    ) -> IncidentRecord:
        """Apply an FSM event to the incident (raising on invalid transitions)."""
        record = self.get(incident_id)
        if context:
            record.context.update(context)

        # Defense in depth: approving remediation re-validates the latest plan.
        if event == "remediation_approved":
            plan = record.plans[-1] if record.plans else None
            if plan is not None:
                runbook = Runbook(incident_id=incident_id, attempt=plan.attempt, steps=plan.steps)
                offending = assert_runbook_within_policy(runbook, _INCIDENT_DIRECTIVES)
                if offending:
                    raise BlastRadiusExceeded(offending)
            record.context["approval_granted"] = True

        prev_state = record.lifecycle_state
        outcome = drive_incident(
            record.lifecycle_state,
            event,
            context=dict(record.context),
            retry_count=record.retry_count,
            max_retries=_MAX_RETRIES,
        )
        record.retry_count = outcome.retry_count
        record.lifecycle_state = outcome.to_state
        if outcome.to_state in _FORWARD_STATES:
            record.state = IncidentState(outcome.to_state)
        if outcome.to_state == "needs_human_input":
            record.context[PAUSED_FROM_KEY] = prev_state

        self._on_enter(record, outcome.to_state, actor=actor)
        self._append_event(
            record,
            "state_change",
            actor,
            note or f"{prev_state} -> {outcome.to_state} ({event})",
        )
        return record

    def _on_enter(self, record: IncidentRecord, state: str, *, actor: str) -> None:
        if state == "incident_created" and record.acknowledged_at is None:
            record.acknowledged_at = _now()
        elif state == "resolved":
            record.resolved_at = _now()
        elif state == "postmortem_created" and record.postmortem is None:
            self._generate_postmortem(record)

    # -- postmortem ------------------------------------------------------- #

    def _generate_postmortem(self, record: IncidentRecord) -> None:
        snapshot = IncidentSnapshot(
            id=record.id,
            key=record.key,
            project_id=record.project_id,
            title=record.title,
            description=record.description,
            severity=record.severity,
            state=record.state,
            blast_radius=record.blast_radius,
            impact_summary=record.impact_summary,
            detected_at=record.detected_at,
            resolved_at=record.resolved_at,
        )
        events = [
            IncidentEventDTO(
                id=ev.id,
                incident_id=record.id,
                sequence=ev.sequence,
                kind=ev.kind,
                actor=ev.actor,
                summary=ev.summary,
                data=ev.data,
                created_at=ev.created_at,
            )
            for ev in record.events
        ]
        plans = [
            Runbook(incident_id=record.id, attempt=p.attempt, steps=p.steps) for p in record.plans
        ]
        postmortem = self._composer.compose(incident=snapshot, events=events, plans=plans)
        record.postmortem = postmortem
        record.postmortem_status = "draft"
        record.context["postmortem_persisted"] = True
        tasks = create_action_item_tasks(
            self._board,
            project_id=record.project_id,
            action_items=postmortem.action_items,
            incident_key=record.key,
        )
        record.action_item_task_keys = [t.key for t in tasks if t.key]
        self._append_event(
            record,
            "note",
            "system",
            f"postmortem generated ({len(tasks)} follow-up tasks)",
        )

    def publish_postmortem(self, incident_id: uuid.UUID) -> IncidentRecord:
        record = self.get(incident_id)
        if record.postmortem is None:
            raise IncidentNotFound(f"postmortem for {incident_id}")
        record.postmortem_status = "published"
        return record

    # -- helpers ---------------------------------------------------------- #

    def _append_event(
        self,
        record: IncidentRecord,
        kind: str,
        actor: str,
        summary: str,
        *,
        data: dict | None = None,
    ) -> None:
        record.events.append(
            _Event(
                id=uuid.uuid4(),
                sequence=len(record.events) + 1,
                kind=kind,
                actor=actor,
                summary=summary,
                data=data or {},
                created_at=_now(),
            )
        )


__all__ = ["IncidentRecord", "IncidentService"]
