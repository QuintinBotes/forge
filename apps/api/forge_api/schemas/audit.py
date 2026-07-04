"""Request/response schemas for the F39 admin audit API (over the canonical
``forge_contracts.audit`` shapes; read-only — there is no write schema)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from forge_contracts.audit import ChainVerifyResult

__all__ = [
    "AuditActionsOut",
    "AuditEntryOut",
    "AuditListResponse",
    "AuditVerifyIn",
    "ChainVerifyResult",
]


class AuditEntryOut(BaseModel):
    """One persisted, redacted audit row including its chain fields."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
    seq: int | None = None
    action: str
    actor_id: UUID | None = None
    actor_type: str
    actor_label: str | None = None
    target_type: str | None = None
    target_id: UUID | None = None
    scope_type: str | None = None
    scope_id: UUID | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    result: str
    severity: str
    reason: str | None = None
    details: dict[str, Any]
    detail_ref: dict[str, Any] | None = None
    request_id: str | None = None
    payload_hash: str | None = None
    prev_hash: str | None = None
    entry_hash: str | None = None
    created_at: datetime


class AuditListResponse(BaseModel):
    items: list[AuditEntryOut]
    next_cursor: str | None = None


class AuditVerifyIn(BaseModel):
    from_seq: int | None = Field(default=None, ge=1)
    to_seq: int | None = Field(default=None, ge=1)


class AuditActionsOut(BaseModel):
    """Filter vocabulary for the audit viewer UI."""

    actions: list[str]
    actor_types: list[str]
    resource_types: list[str]
    outcomes: list[str]
    severities: list[str]
