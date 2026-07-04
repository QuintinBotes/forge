"""Request bodies for the F36 unified approvals router."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from forge_approval.models import GateType, RiskLevel


class CreateApprovalRequest(BaseModel):
    """Body for ``POST /approvals`` — open a gate (producers + synthetic triggers).

    There is deliberately no ``status``/decision field: gates open pending and
    are only ever resolved through the decision endpoint's authorization path.
    """

    gate_type: GateType
    subject_type: str = "workflow_run"
    subject_id: uuid.UUID | None = None
    workflow_run_id: uuid.UUID | None = None
    agent_run_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    requested_actor: str = "system"
    required_approvals: int = 1
    risk_level: RiskLevel = "info"
    title: str | None = None
    gate_payload: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime | None = None


class ApprovalCount(BaseModel):
    """Body of ``GET /approvals/count`` (the nav badge)."""

    count: int


__all__ = ["ApprovalCount", "CreateApprovalRequest"]
