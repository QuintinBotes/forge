"""Request/response models for the PM-adapter router (F18).

These are the API-boundary DTOs. Secrets (API tokens, OAuth bundles, webhook
secrets) are **never** included in any response — only redaction-safe booleans
(``has_credential`` / ``has_webhook_secret``) and the connected account label.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from forge_db.models.enums import (
    PMAuthType,
    PMConflictPolicy,
    PMConnectionStatus,
    PMProvider,
    PMSyncDirection,
    PMSyncState,
)


class PMConnectionResponse(BaseModel):
    """Redaction-safe view of a ``pm_connection`` row."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    provider: PMProvider
    name: str
    project_id: uuid.UUID
    external_base_url: str | None = None
    external_project_key: str
    external_project_id: str
    auth_type: PMAuthType
    account_label: str | None = None
    granted_scopes: list[str] = []
    sync_direction: PMSyncDirection
    conflict_policy: PMConflictPolicy
    status_map: dict = {}
    priority_map: dict = {}
    field_map: dict = {}
    status: PMConnectionStatus
    last_health_at: datetime | None = None
    last_full_sync_at: datetime | None = None
    has_credential: bool = False
    has_webhook_secret: bool = False
    created_at: datetime
    updated_at: datetime


class PMConnectionDetail(PMConnectionResponse):
    link_counts: dict[str, int] = {}


class PMConnectionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    status_map: dict | None = None
    priority_map: dict | None = None
    field_map: dict | None = None
    sync_direction: PMSyncDirection | None = None
    conflict_policy: PMConflictPolicy | None = None
    enabled: bool | None = None


class PMLinkResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    forge_task_id: uuid.UUID
    provider: PMProvider
    external_id: str
    external_key: str
    external_url: str
    sync_state: PMSyncState
    last_synced_at: datetime | None = None
    conflict_detail: dict | None = None


class ResolveConflictRequest(BaseModel):
    winner: Literal["forge", "external"]


class WebhookAck(BaseModel):
    status: str
    delivery_id: str | None = None
