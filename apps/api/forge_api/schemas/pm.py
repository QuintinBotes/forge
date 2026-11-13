"""Request/response models for the PM-adapter router (F18).

These are the API-boundary DTOs. Secrets (API tokens, OAuth bundles, webhook
secrets) are **never** included in any response — only redaction-safe booleans
(``has_credential`` / ``has_webhook_secret``) and the connected account label.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from forge_contracts.pm import StatusCategory
from forge_db.models.enums import (
    PMAuthType,
    PMConflictPolicy,
    PMConnectionStatus,
    PMProvider,
    PMSyncDirection,
    PMSyncState,
)


class PMDispatchConfig(BaseModel):
    """Issue-driven dispatch config (spec M, M3).

    Persisted at ``PMConnection.config["dispatch"]`` and validated here at the
    API boundary (create + patch); the worker re-validates the stored copy and
    fails closed on anything invalid (ADR-0007). ``enabled`` alone is not
    enough to start a run: ``repository`` must be set and connected, and the
    webhook sender must be listed in ``allowed_senders``.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    label: str = Field(default="forge:run", min_length=1, max_length=100)
    repository: str | None = None
    allowed_senders: list[str] = Field(default_factory=list)
    max_active_runs: int = Field(default=2, ge=1, le=10)
    # Spec M (M2): the one tracker status forge may set on its own — the forge
    # status *category* mapped through the connection's status map when a run
    # opens a pull request; ``None`` means forge never transitions the issue.
    status_on_pr_opened: StatusCategory | None = None

    @field_validator("repository")
    @classmethod
    def _repository_is_owner_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        owner, sep, name = value.partition("/")
        if not sep or not owner or not name or "/" in name:
            raise ValueError("repository must be 'owner/name'")
        return value

    def sender_allowed(self, sender_login: str | None) -> bool:
        """Case-insensitive allow-list check; an empty list allows nobody."""
        if not sender_login:
            return False
        return sender_login.lower() in {s.lower() for s in self.allowed_senders}


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
    dispatch: PMDispatchConfig = Field(default_factory=PMDispatchConfig)
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
    dispatch: PMDispatchConfig | None = None
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
