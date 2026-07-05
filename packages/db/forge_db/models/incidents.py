"""Incident-workflow models (F17): alerts, timeline, remediation, postmortem.

These EXTEND the foundation data model with the incident-specific tables the F17
slice owns. They follow the established conventions: singular table names,
:class:`WorkspaceScopedModel` (UUID PK + ``workspace_id`` + timestamps), JSON via
``json_type()``. The incident *lifecycle* FSM state (which spans the 10 forward
states plus the shared ``closed``/``needs_human_input``/``failed``/``cancelled``
error states) is mirrored onto :attr:`Incident.lifecycle_state` (a free string),
since the foundation ``IncidentState`` enum only enumerates the 10 forward states.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from forge_db.base import WorkspaceScopedModel, json_type

#: Partial-index predicate: an incident is "open" while its lifecycle state is
#: not a resolved/closed/terminal state. Used by the dedup uniqueness guard.
_OPEN_INCIDENT_PREDICATE = (
    "dedup_key IS NOT NULL AND (lifecycle_state IS NULL OR lifecycle_state "
    "NOT IN ('resolved', 'postmortem_created', 'closed', 'cancelled', 'failed'))"
)


class IncidentAlert(WorkspaceScopedModel):
    """A raw inbound alert (idempotency + audit; raw payload not persisted)."""

    __tablename__ = "incident_alert"
    __table_args__ = (
        Index(
            "uq_incident_alert_delivery",
            "provider",
            "delivery_id",
            unique=True,
            postgresql_where=text("delivery_id IS NOT NULL"),
            sqlite_where=text("delivery_id IS NOT NULL"),
        ),
    )

    incident_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("incident.id", ondelete="SET NULL"), nullable=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    delivery_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    dedup_key: Mapped[str] = mapped_column(String(256), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="medium")
    title: Mapped[str] = mapped_column(Text, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="received")
    received_at: Mapped[datetime | None] = mapped_column(nullable=True)


class IncidentEvent(WorkspaceScopedModel):
    """An append-only incident timeline event (insert-only in the repository)."""

    __tablename__ = "incident_event"
    __table_args__ = (UniqueConstraint("incident_id", "sequence"),)

    incident_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("incident.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workflow_run.id", ondelete="SET NULL"), nullable=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="note")
    actor: Mapped[str] = mapped_column(String(128), nullable=False, default="system")
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    data: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict, nullable=False)


class RemediationPlan(WorkspaceScopedModel):
    """A proposed/approved remediation runbook for an incident."""

    __tablename__ = "remediation_plan"

    incident_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("incident.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workflow_run.id", ondelete="SET NULL"), nullable=True
    )
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_run.id", ondelete="SET NULL"), nullable=True
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    max_blast_radius: Mapped[str] = mapped_column(String(16), nullable=False, default="low")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="proposed")
    steps: Mapped[list[Any]] = mapped_column(json_type(), default=list, nullable=False)


class Postmortem(WorkspaceScopedModel):
    """A composed postmortem document for a resolved incident (1:1)."""

    __tablename__ = "postmortem"

    incident_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("incident.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    content_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    storage_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    root_cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    data: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict, nullable=False)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )


class PostmortemActionItem(WorkspaceScopedModel):
    """A postmortem action item, linked to the board Task it created."""

    __tablename__ = "postmortem_action_item"

    postmortem_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("postmortem.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("task.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="chore")
    priority: Mapped[str] = mapped_column(String(8), nullable=False, default="medium")
    owner_hint: Mapped[str | None] = mapped_column(String(128), nullable=True)


def open_incident_dedup_index() -> Index:
    """Build the partial unique index enforcing one OPEN incident per dedup key.

    Defined as a factory so it binds to the ``incident`` table via
    ``Incident.__table_args__`` (a module-level ``Index`` with string column names
    would not associate with any table).
    """
    return Index(
        "uq_incident_open_dedup",
        "workspace_id",
        "dedup_key",
        unique=True,
        postgresql_where=text(_OPEN_INCIDENT_PREDICATE),
        sqlite_where=text(_OPEN_INCIDENT_PREDICATE),
    )


__all__ = [
    "IncidentAlert",
    "IncidentEvent",
    "Postmortem",
    "PostmortemActionItem",
    "RemediationPlan",
    "open_incident_dedup_index",
]
