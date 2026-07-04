"""Deployment FSM states, events, and the event envelope.

Enums are the canonical ones from :mod:`forge_contracts.deployment` (also mirrored
into ``forge_db`` columns); re-exported here so the engine has a single import.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from forge_contracts.deployment import (
    TERMINAL_STATES,
    DeploymentEventType,
    DeploymentKind,
    DeploymentState,
    DeploymentTrigger,
    GateCheckName,
    GateCheckStatus,
    HealthStatus,
)


class DeploymentEvent(BaseModel):
    """An event driving a single :class:`DeploymentStateMachine` transition."""

    model_config = ConfigDict(extra="forbid")

    type: DeploymentEventType
    payload: dict[str, Any] = Field(default_factory=dict)
    actor: str = "system"
    idempotency_key: str | None = None


__all__ = [
    "TERMINAL_STATES",
    "DeploymentEvent",
    "DeploymentEventType",
    "DeploymentKind",
    "DeploymentState",
    "DeploymentTrigger",
    "GateCheckName",
    "GateCheckStatus",
    "HealthStatus",
]
