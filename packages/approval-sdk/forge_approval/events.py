"""Typed activity-bus events — the ONLY two consumer-facing event types F36
introduces (F16 Slack / F01 board timeline subscribe to exactly these).

``deploy.approved`` / ``policy_override.granted`` are NOT separate event types:
they ride on ``approval.resolved`` plus the gate hook's domain signal.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel

from forge_approval.models import GateStatus, GateType, ResolutionOutcome

APPROVAL_REQUESTED_TOPIC = "approval.requested"
APPROVAL_RESOLVED_TOPIC = "approval.resolved"


class ApprovalRequestedEvent(BaseModel):
    approval_id: UUID
    workspace_id: UUID
    project_id: UUID | None = None
    gate_type: GateType
    subject_type: str
    subject_id: UUID | None = None
    risk_level: str = "info"
    requested_actor: str = "system"
    requested_at: datetime | None = None


class ApprovalResolvedEvent(BaseModel):
    approval_id: UUID
    workspace_id: UUID
    gate_type: GateType
    status: GateStatus
    resolver_user_id: UUID | None = None
    outcome: ResolutionOutcome
    resolved_at: datetime | None = None


@runtime_checkable
class ActivityBus(Protocol):
    """Where ``approval.*`` events are published (F01 bus / F16 queue)."""

    def publish(self, topic: str, event: BaseModel) -> None: ...


class InMemoryActivityBus:
    """Recording bus for tests and the in-process composition root."""

    def __init__(self) -> None:
        self.published: list[tuple[str, BaseModel]] = []

    def publish(self, topic: str, event: BaseModel) -> None:
        self.published.append((topic, event))

    def by_topic(self, topic: str) -> list[BaseModel]:
        return [event for t, event in self.published if t == topic]


__all__ = [
    "APPROVAL_REQUESTED_TOPIC",
    "APPROVAL_RESOLVED_TOPIC",
    "ActivityBus",
    "ApprovalRequestedEvent",
    "ApprovalResolvedEvent",
    "InMemoryActivityBus",
]
