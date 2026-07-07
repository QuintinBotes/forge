"""External PM-adapter contract surface for F18 (Jira, Linear).

This module is an **additive extension** of the frozen ``forge_contracts``
surface. The top-level package already exposes a *v1* ``ExternalTask`` /
``ForgeTask`` / ``WebhookEvent`` / ``HealthResult`` / ``PMAdapter`` used by the
generic ``forge_integrations.pm_adapter`` surface. F18 needs a richer, async,
provider-agnostic contract (status *categories*, durable links, conflict
resolution, webhook verification), so the v2 DTOs and the async ``PMAdapter``
Protocol live here in their own module namespace (``forge_contracts.pm.*``) and
do **not** mutate the frozen top-level ``__all__``.

The ``Direction`` enum is reused verbatim from :mod:`forge_contracts.enums`
(``IN`` / ``OUT``) so the two surfaces interoperate.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from forge_contracts.enums import Direction

__all__ = [
    "AdapterContext",
    "ConflictPolicy",
    "Direction",
    "ExternalTask",
    "ForgePriority",
    "ForgeTask",
    "HealthResult",
    "HttpResponse",
    "PMAdapter",
    "PMAuthType",
    "PMConnectionConfig",
    "PMConnectionStatus",
    "PMDeliveryStatus",
    "PMProvider",
    "PMSyncState",
    "PMTransport",
    "StatusCategory",
    "SyncDirection",
    "SyncOutcome",
    "WebhookEvent",
]


# --------------------------------------------------------------------------- #
# Enums                                                                        #
# --------------------------------------------------------------------------- #


class PMProvider(StrEnum):
    """Supported external project-management providers (F18: Jira, Linear)."""

    jira = "jira"
    linear = "linear"


class SyncDirection(StrEnum):
    """Per-connection sync direction policy."""

    bidirectional = "bidirectional"
    inbound_only = "inbound_only"
    outbound_only = "outbound_only"


class ConflictPolicy(StrEnum):
    """How concurrent both-sides edits are resolved."""

    forge_wins = "forge_wins"
    external_wins = "external_wins"
    newest_wins = "newest_wins"
    manual = "manual"


class StatusCategory(StrEnum):
    """Normalized status categories (aligns with board status semantics)."""

    backlog = "backlog"
    unstarted = "unstarted"
    started = "started"
    completed = "completed"
    canceled = "canceled"


class ForgePriority(StrEnum):
    """Normalized Forge priority tokens for PM mapping."""

    none = "none"
    low = "low"
    medium = "medium"
    high = "high"
    urgent = "urgent"


class PMAuthType(StrEnum):
    oauth = "oauth"
    api_token = "api_token"


class PMConnectionStatus(StrEnum):
    pending = "pending"
    connected = "connected"
    error = "error"
    disabled = "disabled"


class PMSyncState(StrEnum):
    synced = "synced"
    pending_out = "pending_out"
    pending_in = "pending_in"
    conflict = "conflict"
    error = "error"


class PMDeliveryStatus(StrEnum):
    received = "received"
    processed = "processed"
    skipped = "skipped"
    echo_suppressed = "echo_suppressed"
    error = "error"


# --------------------------------------------------------------------------- #
# DTOs                                                                         #
# --------------------------------------------------------------------------- #


class _PMModel(BaseModel):
    """Shared base: populatable by field name, tolerant of unknown keys."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class ExternalTask(_PMModel):
    """A task in an external PM system, normalized to the F18 sync grain."""

    provider: PMProvider
    external_id: str  # stable id (Jira issue id / Linear issue uuid)
    external_key: str  # human key (ENG-123 / ENG-45)
    url: str
    title: str
    description_md: str | None = None  # normalized to markdown (ADF/GraphQL decoded)
    status_name: str  # raw external status / workflow-state name
    status_category: StatusCategory | None = None
    priority_token: str | None = None  # raw external priority token
    assignee_external_id: str | None = None
    assignee_email: str | None = None
    labels: list[str] = Field(default_factory=list)
    external_updated_at: datetime
    raw: dict = Field(default_factory=dict)  # redacted provider extras for field_map


class ForgeTask(_PMModel):
    """Stable serialization of a board task for PM sync."""

    id: UUID
    key: str
    project_id: UUID
    title: str
    description_md: str | None = None
    status_id: UUID | None = None
    status_category: StatusCategory
    priority: ForgePriority
    assignee_id: UUID | None = None
    assignee_email: str | None = None
    label_names: list[str] = Field(default_factory=list)
    version: int = 0
    updated_at: datetime


class WebhookEvent(_PMModel):
    """A normalized inbound PM webhook (a *hint*; state is always re-fetched)."""

    provider: PMProvider
    delivery_id: str
    event_type: Literal["issue.created", "issue.updated", "issue.deleted"]
    external_id: str | None = None
    external_key: str | None = None
    signature_valid: bool = False
    received_at: datetime
    payload: dict = Field(default_factory=dict)  # parsed, secret-free subset


class HealthResult(_PMModel):
    status: Literal["connected", "error"]
    provider: PMProvider
    latency_ms: float = 0.0
    account: str | None = None
    granted_scopes: list[str] = Field(default_factory=list)
    error: str | None = None  # redacted


class SyncOutcome(_PMModel):
    direction: Direction
    forge_task_id: UUID | None = None
    external_id: str | None = None
    action: Literal["created", "updated", "no_change", "skipped_echo", "conflict", "error"]
    winner: Literal["forge", "external"] | None = None
    detail: dict = Field(default_factory=dict)


class AdapterContext(_PMModel):
    """Everything an adapter needs to talk to one external project."""

    connection_id: UUID
    workspace_id: UUID
    provider: PMProvider
    external_project_key: str
    external_project_id: str
    external_base_url: str | None = None
    status_map: dict = Field(default_factory=dict)
    priority_map: dict = Field(default_factory=dict)
    field_map: dict = Field(default_factory=dict)
    config: dict = Field(default_factory=dict)


class PMConnectionConfig(_PMModel):
    """Request body for ``POST /connections`` / OSS connector-template YAML."""

    provider: PMProvider
    name: str
    project_id: UUID
    external_base_url: str | None = None
    external_project_key: str
    auth_type: Literal["oauth", "api_token"] = "oauth"
    api_token: str | None = None  # stored to vault, never returned
    api_token_email: str | None = None  # Jira Basic (email + token)
    sync_direction: SyncDirection = SyncDirection.bidirectional
    conflict_policy: ConflictPolicy = ConflictPolicy.newest_wins
    status_map: dict = Field(default_factory=dict)
    priority_map: dict = Field(default_factory=dict)
    field_map: dict = Field(default_factory=dict)
    on_external_delete: Literal["unlink", "archive"] = "unlink"


# --------------------------------------------------------------------------- #
# Transport                                                                    #
# --------------------------------------------------------------------------- #


class HttpResponse(_PMModel):
    status_code: int
    json_body: dict | list | None = None
    headers: dict[str, str] = Field(default_factory=dict)


@runtime_checkable
class PMTransport(Protocol):
    """Offline-testable HTTP/GraphQL transport (mirrors F03's GitHubTransport)."""

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        json: dict | None = None,
        params: dict | None = None,
    ) -> HttpResponse: ...


# --------------------------------------------------------------------------- #
# Adapter Protocol (async, implemented form of the spec contract)             #
# --------------------------------------------------------------------------- #


@runtime_checkable
class PMAdapter(Protocol):
    """Provider-specific surface; the single OSS extension point.

    The spec's ``sync_in`` / ``sync_out`` / ``subscribe`` are realized by
    :class:`forge_integrations.pm.sync_engine.PMSyncEngine`, which composes the
    pure mapping methods with the external-I/O methods below. Mapping stays sync
    (pure functions); I/O is async.
    """

    provider: PMProvider

    # --- mapping (pure; honor the spec signatures) ---
    def map_status(self, value: str, direction: Direction) -> str: ...
    def map_priority(self, value: str, direction: Direction) -> str: ...
    def map_fields(self, fields: dict, direction: Direction) -> dict: ...

    # --- external I/O ---
    async def fetch_external(self, external_id: str) -> ExternalTask: ...
    async def create_external(self, forge_task: ForgeTask) -> ExternalTask: ...
    async def update_external(self, external_id: str, forge_task: ForgeTask) -> ExternalTask: ...
    async def list_external(
        self, *, cursor: str | None = None, limit: int = 50
    ) -> tuple[list[ExternalTask], str | None]: ...

    # --- webhook + health ---
    def parse_webhook(self, body: bytes, headers: dict[str, str]) -> WebhookEvent: ...
    def verify_webhook(self, body: bytes, headers: dict[str, str], secret: str) -> bool: ...
    async def register_webhook(self, callback_url: str, secret: str) -> str: ...
    async def unregister_webhook(self, external_webhook_id: str) -> None: ...
    async def get_connection_health(self) -> HealthResult: ...
