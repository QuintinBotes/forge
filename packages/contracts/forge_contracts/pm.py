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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from forge_contracts.enums import Direction

__all__ = [
    "AdapterContext",
    "ConflictPolicy",
    "Direction",
    "ExternalTask",
    "ForgePriority",
    "ForgeTask",
    "GenericAdapterConfig",
    "GenericEndpointConfig",
    "GenericWebhookConfig",
    "GenericWebhookSignatureAlgo",
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
    """Supported external project-management providers.

    F18 shipped Jira + Linear; F40-PM-ADAPTERS-1 adds Asana, Monday.com, and
    GitHub Projects (v2); F40-PM-ADAPTERS-2 adds ClickUp, Trello, GitLab
    Issues, and a config-driven ``generic`` (BYO-board) connector — all behind
    the same ``PMAdapter`` seam.
    """

    jira = "jira"
    linear = "linear"
    asana = "asana"
    monday = "monday"
    github_projects = "github_projects"
    clickup = "clickup"
    trello = "trello"
    gitlab = "gitlab"
    generic = "generic"


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
# Generic / BYO-board connector (F40-PM-ADAPTERS-2)                            #
# --------------------------------------------------------------------------- #


class GenericWebhookSignatureAlgo(StrEnum):
    """Supported webhook-verification schemes for a BYO board."""

    none = "none"
    hmac_sha256_hex = "hmac_sha256_hex"
    hmac_sha1_base64 = "hmac_sha1_base64"
    shared_secret_header = "shared_secret_header"  # plain constant-time equality


class GenericWebhookConfig(_PMModel):
    """Declarative webhook shape for the generic connector.

    ``*_path`` fields are dotted paths (``"a.b.c"``) into the parsed JSON
    webhook body; an empty string means "the root object itself".
    """

    signature_header: str | None = None
    signature_algo: GenericWebhookSignatureAlgo = GenericWebhookSignatureAlgo.none
    event_type_path: str = "event_type"
    external_id_path: str = "id"
    delivery_id_header: str | None = None
    event_type_map: dict[str, Literal["issue.created", "issue.updated", "issue.deleted"]] = Field(
        default_factory=dict
    )
    default_event_type: Literal["issue.created", "issue.updated", "issue.deleted"] = "issue.updated"


class GenericEndpointConfig(_PMModel):
    """URL path templates for the generic connector.

    Templates may reference ``{external_id}``, ``{project_id}``, ``{cursor}``,
    ``{limit}``, and ``{webhook_id}`` — unused placeholders are simply not
    substituted for a given call.
    """

    get: str
    create: str
    update: str
    list: str
    # Optional dedicated status-change call for boards that require a
    # workflow-validated transition rather than a plain field write on the
    # ``update`` endpoint (the Jira-shaped case). When set, the adapter posts
    # the mapped status there instead of folding it into the ``update`` body.
    transition: str | None = None
    register_webhook: str | None = None
    unregister_webhook: str | None = None
    me: str | None = None


# The forge-side fields a BYO board can populate via a dotted response path.
# (The item's own id/key/url are covered separately by ``item_id_path`` /
# ``item_key_path`` / ``item_url_path`` below, since they are also needed
# outside the ``ExternalTask`` shape — e.g. to address the update/get
# endpoints after a create.)
_GENERIC_KNOWN_FIELDS: frozenset[str] = frozenset(
    {
        "title",
        "status",
        "description_md",
        "priority_token",
        "assignee_email",
        "labels",
        "external_updated_at",
    }
)


class GenericAdapterConfig(_PMModel):
    """Declarative mapping that drives :class:`GenericAdapter` — no code change.

    A user brings a board Forge has no native adapter for by supplying this
    config (persisted on ``AdapterContext.config["generic_config"]``): a base
    URL, endpoint templates, a field-map (forge field -> dotted JSON path),
    and a status/priority-category map from provider-native values to the
    agnostic Forge tokens. Validated eagerly so a bad config is rejected at
    connection-setup time, not at first sync (status/priority mapping never
    silently drops a value — see ``forge_integrations.pm.base``).
    """

    base_url: str
    auth_header_name: str = "Authorization"
    endpoints: GenericEndpointConfig
    fields: dict[str, str]  # forge field name -> dotted external JSON path
    status_map: dict[str, str]  # forge StatusCategory value -> external status token (OUT)
    priority_map: dict[str, str] = Field(default_factory=dict)  # forge priority -> token (OUT)
    list_items_path: str = ""  # dotted path to the array in a list response ("" = root array)
    item_id_path: str = "id"
    item_key_path: str = "id"
    item_url_path: str = "url"
    webhook: GenericWebhookConfig = Field(default_factory=GenericWebhookConfig)

    @field_validator("base_url")
    @classmethod
    def _base_url_is_http(cls, value: str) -> str:
        if not value or not value.startswith(("http://", "https://")):
            raise ValueError(f"base_url must be an http(s) URL, got {value!r}")
        return value.rstrip("/")

    @field_validator("fields")
    @classmethod
    def _fields_cover_required(cls, value: dict[str, str]) -> dict[str, str]:
        unknown = set(value) - _GENERIC_KNOWN_FIELDS
        if unknown:
            raise ValueError(f"fields has unknown forge field name(s): {sorted(unknown)}")
        missing = {"title", "status"} - set(value)
        if missing:
            raise ValueError(f"fields is missing required mapping(s): {sorted(missing)}")
        for name, path in value.items():
            if not path:
                raise ValueError(f"fields[{name!r}] must be a non-empty dotted path")
        return value

    @field_validator("status_map")
    @classmethod
    def _status_map_covers_every_category(cls, value: dict[str, str]) -> dict[str, str]:
        required = {c.value for c in StatusCategory}
        missing = required - set(value)
        if missing:
            raise ValueError(f"status_map must map every StatusCategory; missing {sorted(missing)}")
        empty = [k for k, v in value.items() if not v]
        if empty:
            raise ValueError(f"status_map has empty external value(s) for: {sorted(empty)}")
        return value

    @model_validator(mode="after")
    def _endpoints_non_empty(self) -> GenericAdapterConfig:
        for name in ("get", "create", "update", "list"):
            if not getattr(self.endpoints, name):
                raise ValueError(f"endpoints.{name} must be a non-empty path template")
        return self

    @model_validator(mode="after")
    def _priority_map_covers_every_priority_when_field_mapped(self) -> GenericAdapterConfig:
        # priority_map is only ever consumed (via BaseAdapter.resolve_value, which
        # raises MappingError with no fallback) when the config maps a
        # "priority_token" field -- exactly mirroring status_map's eager,
        # fail-closed completeness requirement above, so a bad config is
        # rejected at connection-setup time rather than crashing uncaught at
        # first write.
        if "priority_token" not in self.fields:
            return self
        required = {p.value for p in ForgePriority}
        missing = required - set(self.priority_map)
        if missing:
            raise ValueError(
                "priority_map must map every ForgePriority when 'priority_token' is in "
                f"fields; missing {sorted(missing)}"
            )
        empty = [k for k, v in self.priority_map.items() if not v]
        if empty:
            raise ValueError(f"priority_map has empty external value(s) for: {sorted(empty)}")
        return self


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
