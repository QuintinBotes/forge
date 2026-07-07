"""Incident router (F17) — the incident-workflow surface over HTTP.

Serves declare/list/get/timeline/event/remediation/postmortem. Handlers delegate
to a process-wide per-workspace :class:`IncidentService` (Phase-1 in-memory; the
DB-backed implementation swaps in behind the same dependency). Tenant isolation
is per-workspace (a foreign id is 404, no existence leak). RBAC: reads need READ
(any role); declaring an incident and driving FSM events need WRITE (member /
admin) — so the read-only ``viewer`` and the ``agent-runner`` (which lacks WRITE)
can never approve a remediation, preserving the human-in-the-loop guarantee.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from forge_api.auth.rbac import Permission
from forge_api.deps import Principal, get_current_principal
from forge_api.routers._rbac import require_permission
from forge_api.schemas.incidents import (
    IncidentDeclareRequest,
    IncidentDetailView,
    IncidentEventRequest,
    IncidentEventView,
    IncidentView,
    PostmortemView,
    ProposeRemediationRequest,
    RemediationPlanView,
    RemediationStepView,
)
from forge_api.services.incident_service import IncidentRecord, IncidentService
from forge_board.incidents.errors import (
    BlastRadiusExceeded,
    IncidentNotFound,
)
from forge_workflow import InvalidTransitionError

router = APIRouter(
    prefix="/incidents",
    tags=["incidents"],
    dependencies=[Depends(get_current_principal)],
)

ReaderDep = Annotated[Principal, Depends(require_permission(Permission.READ))]
WriterDep = Annotated[Principal, Depends(require_permission(Permission.WRITE))]


# --------------------------------------------------------------------------- #
# Per-workspace service registry                                               #
# --------------------------------------------------------------------------- #


class IncidentServiceRegistry:
    """Vends an :class:`IncidentService` per workspace (tenant isolation)."""

    def __init__(self) -> None:
        self._services: dict[uuid.UUID, IncidentService] = {}

    def for_workspace(self, workspace_id: uuid.UUID) -> IncidentService:
        service = self._services.get(workspace_id)
        if service is None:
            service = IncidentService()
            self._services[workspace_id] = service
        return service


@lru_cache(maxsize=1)
def _incident_registry_singleton() -> IncidentServiceRegistry:
    return IncidentServiceRegistry()


def get_incident_registry() -> IncidentServiceRegistry:
    """Return the process-wide incident registry (override in tests via DI)."""
    return _incident_registry_singleton()


def get_incident_service(
    principal: Annotated[Principal, Depends(get_current_principal)],
    registry: Annotated[IncidentServiceRegistry, Depends(get_incident_registry)],
) -> IncidentService:
    """Return the incident service scoped to the caller's workspace."""
    return registry.for_workspace(principal.workspace_id)


ServiceDep = Annotated[IncidentService, Depends(get_incident_service)]


# --------------------------------------------------------------------------- #
# Error mapping + serialization                                               #
# --------------------------------------------------------------------------- #


@contextmanager
def _incident_errors() -> Iterator[None]:
    try:
        yield
    except IncidentNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except BlastRadiusExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": str(exc), "offending_step_ids": exc.offending_step_ids},
        ) from exc
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


def _plan_view(service: IncidentService, record: IncidentRecord) -> RemediationPlanView | None:
    plan = record.plans[-1] if record.plans else None
    if plan is None:
        return None
    steps = [
        RemediationStepView(
            id=s.id,
            order=s.order,
            title=s.title,
            action=s.action,
            blast_radius=s.blast_radius,
            rationale=s.rationale,
            status=s.status,
            blocked=s.id in plan.offending_step_ids,
        )
        for s in plan.steps
    ]
    return RemediationPlanView(
        id=plan.id,
        incident_id=record.id,
        attempt=plan.attempt,
        max_blast_radius=plan.max_blast_radius,
        status=plan.status,
        steps=steps,
        offending_step_ids=plan.offending_step_ids,
    )


def _view(service: IncidentService, record: IncidentRecord) -> IncidentView:
    return IncidentView(
        id=record.id,
        key=record.key,
        project_id=record.project_id,
        title=record.title,
        description=record.description,
        severity=record.severity,
        state=record.state,
        lifecycle_state=record.lifecycle_state,
        source=record.source,
        dedup_key=record.dedup_key,
        commander_id=record.commander_id,
        blast_radius=record.blast_radius,
        impact_summary=record.impact_summary,
        created_at=record.created_at,
        detected_at=record.detected_at,
        acknowledged_at=record.acknowledged_at,
        resolved_at=record.resolved_at,
        allowed_events=service.allowed_events(record),
    )


def _detail(service: IncidentService, record: IncidentRecord) -> IncidentDetailView:
    base = _view(service, record).model_dump()
    return IncidentDetailView(
        **base,
        remediation_plan=_plan_view(service, record),
        event_count=len(record.events),
    )


# --------------------------------------------------------------------------- #
# Routes                                                                      #
# --------------------------------------------------------------------------- #


@router.post("", response_model=IncidentView, status_code=status.HTTP_201_CREATED)
def declare_incident(
    service: ServiceDep, principal: WriterDep, body: IncidentDeclareRequest
) -> IncidentView:
    """Declare a manual incident (FSM starts at ``incident_created``)."""
    record = service.declare(
        project_id=body.project_id,
        title=body.title,
        severity=body.severity,
        description=body.description,
        repo_id=body.repo_id,
        commander_id=body.commander_id,
        actor=f"user:{principal.user_id}",
    )
    return _view(service, record)


@router.get("", response_model=list[IncidentView])
def list_incidents(
    service: ServiceDep,
    principal: ReaderDep,
    project_id: Annotated[uuid.UUID | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    severity: Annotated[str | None, Query()] = None,
) -> list[IncidentView]:
    from forge_contracts.enums import IncidentSeverity

    sev = IncidentSeverity(severity) if severity else None
    records = service.list(project_id=project_id, state=state, severity=sev)
    return [_view(service, r) for r in records]


@router.get("/{incident_id}", response_model=IncidentDetailView)
def get_incident(
    service: ServiceDep, principal: ReaderDep, incident_id: uuid.UUID
) -> IncidentDetailView:
    with _incident_errors():
        return _detail(service, service.get(incident_id))


@router.get("/{incident_id}/timeline", response_model=list[IncidentEventView])
def incident_timeline(
    service: ServiceDep, principal: ReaderDep, incident_id: uuid.UUID
) -> list[IncidentEventView]:
    with _incident_errors():
        events = service.timeline(incident_id)
    return [
        IncidentEventView(
            id=ev.id,
            incident_id=incident_id,
            sequence=ev.sequence,
            kind=ev.kind,
            actor=ev.actor,
            summary=ev.summary,
            data=ev.data,
            created_at=ev.created_at,
        )
        for ev in events
    ]


@router.post("/{incident_id}/events", response_model=IncidentDetailView)
def send_incident_event(
    service: ServiceDep,
    principal: WriterDep,
    incident_id: uuid.UUID,
    body: IncidentEventRequest,
) -> IncidentDetailView:
    """Drive the incident FSM with an event (WRITE-gated: human-in-the-loop)."""
    with _incident_errors():
        record = service.send_event(
            incident_id,
            body.event,
            actor=f"user:{principal.user_id}",
            context=body.context,
            note=body.note,
        )
        return _detail(service, record)


@router.post("/{incident_id}/remediation", response_model=RemediationPlanView)
def propose_remediation(
    service: ServiceDep,
    principal: WriterDep,
    incident_id: uuid.UUID,
    body: ProposeRemediationRequest,
) -> RemediationPlanView:
    """Propose a remediation runbook (validated against the blast-radius posture)."""
    with _incident_errors():
        service.propose_remediation(
            incident_id, steps=body.steps, actor=f"user:{principal.user_id}"
        )
        plan = _plan_view(service, service.get(incident_id))
    assert plan is not None
    return plan


@router.get("/{incident_id}/remediation", response_model=RemediationPlanView)
def get_remediation_plan(
    service: ServiceDep, principal: ReaderDep, incident_id: uuid.UUID
) -> RemediationPlanView:
    with _incident_errors():
        record = service.get(incident_id)
    plan = _plan_view(service, record)
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no remediation plan")
    return plan


@router.get("/{incident_id}/postmortem", response_model=PostmortemView)
def get_postmortem(
    service: ServiceDep, principal: ReaderDep, incident_id: uuid.UUID
) -> PostmortemView:
    with _incident_errors():
        record = service.get(incident_id)
    if record.postmortem is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no postmortem")
    from forge_board.incidents import render_postmortem_md

    return PostmortemView(
        id=record.id,
        incident_id=record.id,
        status=record.postmortem_status,
        content_md=render_postmortem_md(record.postmortem),
        root_cause=record.postmortem.root_cause,
        action_item_task_keys=record.action_item_task_keys,
    )


@router.post("/{incident_id}/postmortem/publish", response_model=PostmortemView)
def publish_postmortem(
    service: ServiceDep, principal: WriterDep, incident_id: uuid.UUID
) -> PostmortemView:
    with _incident_errors():
        record = service.publish_postmortem(incident_id)
    from forge_board.incidents import render_postmortem_md

    assert record.postmortem is not None
    return PostmortemView(
        id=record.id,
        incident_id=record.id,
        status=record.postmortem_status,
        content_md=render_postmortem_md(record.postmortem),
        root_cause=record.postmortem.root_cause,
        action_item_task_keys=record.action_item_task_keys,
    )


__all__ = [
    "IncidentServiceRegistry",
    "get_incident_registry",
    "get_incident_service",
    "router",
]
