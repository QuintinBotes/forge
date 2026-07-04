"""Canonical immutable audit-log contract (``cross-cutting/F39-audit-log``).

F30 bootstrapped this module with the minimal ``AuditEvent`` DTO + ``AuditSink``
Protocol it needed; F39 promotes it to the canonical, frozen audit contract:

- the shared producer DTO :class:`AuditEvent` (extended in place with the F39
  severity / actor-label / correlation fields — all optional, so every existing
  producer keeps working unchanged),
- the persisted read model :class:`AuditEntry` (chain fields included),
- the canonical vocabulary enums (:class:`ActorType`, :class:`AuditAction`,
  :class:`AuditResourceType`, :class:`AuditOutcome`, :class:`AuditSeverity`),
- the pure, deterministic hash helpers shared by the writer, the verifier, and
  offline auditors (:func:`canonical_json`, :func:`compute_payload_hash`,
  :func:`compute_entry_hash`, :data:`GENESIS_HASH`), and
- :class:`ChainVerifyResult`, the verdict of a chain re-walk.

Foundation deviations from the idealized slice doc (deliberate — the in-tree
F30/F37 foundation predates the doc and every producer already conforms):

- ``AuditEvent`` keeps the foundation field names (``target_type``/``target_id``
  /``scope_*``/``before``/``after``/``result``/``details``) instead of the doc's
  ``resource_type``/``resource_id``/``outcome``/``metadata``. The enums below
  are the canonical *vocabulary*; the DTO fields stay ``str`` so producer-owned
  actions (e.g. F30's ``role_grant.*``) remain valid without enum churn.
- ``AuditSink.emit(event)`` keeps the foundation signature (the sink owns its
  session); the doc's ``emit(session, event)`` variant is the concrete
  ``SqlAuditWriter`` constructor instead.
- ``occurred_at`` is the existing ``created_at`` field (caller-supplied event
  time; the writer stamps insert time when absent).

The ORM ``AuditLog``/``AuditChainHead`` models + ``SqlAuditWriter`` +
``verify_chain`` live in ``forge_db.audit``; the query API lives in
``forge_api``.
"""

from __future__ import annotations

import enum
import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "GENESIS_HASH",
    "ActorType",
    "AuditAction",
    "AuditEntry",
    "AuditEvent",
    "AuditOutcome",
    "AuditResourceType",
    "AuditSeverity",
    "AuditSink",
    "ChainVerifyResult",
    "canonical_json",
    "compute_entry_hash",
    "compute_payload_hash",
]

#: ``prev_hash`` of the first entry in every per-workspace chain.
GENESIS_HASH = "0" * 64


class ActorType(enum.StrEnum):
    """Who performed the audited action."""

    USER = "user"
    AGENT_RUNNER = "agent_runner"
    SYSTEM = "system"
    INTEGRATION = "integration"
    # Foundation value emitted by F37's ``actor_type_for`` for non-agent keys.
    API_KEY = "api_key"


class AuditOutcome(enum.StrEnum):
    """Result vocabulary (foundation default is ``success``)."""

    SUCCESS = "success"
    DENIED = "denied"
    ERROR = "error"
    BLOCKED = "blocked"


class AuditSeverity(enum.StrEnum):
    """Alerting/fail-closed tier of an audit event."""

    INFO = "info"
    NOTICE = "notice"
    WARNING = "warning"
    CRITICAL = "critical"


class AuditResourceType(enum.StrEnum):
    """Canonical ``target_type`` vocabulary for cross-slice consistency."""

    TASK = "task"
    WORKFLOW_RUN = "workflow_run"
    AGENT_RUN = "agent_run"
    PULL_REQUEST = "pull_request"
    MCP_CONNECTION = "mcp_connection"
    REPOSITORY = "repository"
    API_KEY = "api_key"
    USER = "user"
    POLICY = "policy"
    SPEC = "spec"
    APPROVAL = "approval"
    AUDIT = "audit"
    SYSTEM = "system"


class AuditAction(enum.StrEnum):
    """Core cross-cutting action vocabulary (Security: "every agent action,
    tool call, MCP call, and approval"). Producer slices may emit additional
    dotted-string actions (e.g. F30's ``role_grant.*``) — the ``action`` field
    stays ``str`` and this enum is the shared core, not a closed set."""

    # agent / tools
    AGENT_ACTION = "agent.action"
    TOOL_CALL = "tool.call"
    # MCP
    MCP_TOOL_CALL = "mcp.tool_call"
    MCP_RESOURCE_READ = "mcp.resource_read"
    MCP_WRITE_BLOCKED = "mcp.write_blocked"
    # policy
    POLICY_TOOL_ALLOWED = "policy.tool_allowed"
    POLICY_TOOL_DENIED = "policy.tool_denied"
    POLICY_OVERRIDE = "policy.override"
    # approvals
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_DECIDED = "approval.decided"
    # workflow lifecycle
    WORKFLOW_TRANSITION = "workflow.transition"
    # secrets / connections / rbac / auth
    APIKEY_CREATED = "apikey.created"
    APIKEY_ROTATED = "apikey.rotated"
    APIKEY_REVOKED = "apikey.revoked"
    SECRET_ACCESSED = "secret.accessed"
    CONNECTION_CREATED = "connection.created"
    CONNECTION_UPDATED = "connection.updated"
    CONNECTION_DELETED = "connection.deleted"
    RBAC_ROLE_CHANGED = "rbac.role_changed"
    AUTH_LOGIN = "auth.login"
    AUTH_LOGOUT = "auth.logout"
    AUTH_FAILED = "auth.failed"
    # audit self-events
    AUDIT_EXPORTED = "audit.exported"
    AUDIT_CHAIN_BROKEN = "audit.chain_broken"


class AuditEvent(BaseModel):
    """A single immutable audit record.

    ``action`` is a dotted vocabulary string (e.g. ``role_grant.created``,
    ``approval.decided``); ``before``/``after`` capture the change for grant/role
    mutations; ``details`` carries any extra structured context.

    F39 additions (all optional; the writer redacts + persists them):
    ``actor_label`` (durable actor snapshot that survives user deletion),
    ``severity`` (alerting tier), ``reason`` (short redacted human reason),
    ``detail_ref`` (``{"table": ..., "id": ...}`` drill-down pointer into the
    per-domain detail row), and ``request_id`` (correlation id).
    """

    model_config = ConfigDict(frozen=True)

    workspace_id: UUID
    action: str
    actor_id: UUID | None = None
    actor_type: str = "user"
    actor_label: str | None = None
    target_type: str | None = None
    target_id: UUID | None = None
    scope_type: str | None = None
    scope_id: UUID | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    result: str = "success"
    severity: str = AuditSeverity.INFO.value
    reason: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    detail_ref: dict[str, str] | None = None
    request_id: str | None = None
    created_at: datetime | None = None


class AuditEntry(AuditEvent):
    """Read model: a persisted, redacted row including its chain fields."""

    id: UUID
    seq: int | None = None
    payload_hash: str | None = None
    prev_hash: str | None = None
    entry_hash: str | None = None
    created_at: datetime


class ChainVerifyResult(BaseModel):
    """Verdict of re-walking one workspace's audit hash chain."""

    workspace_id: UUID
    ok: bool
    entries_checked: int
    broken_at_seq: int | None = None
    detail: str | None = None


@runtime_checkable
class AuditSink(Protocol):
    """A durable, append-only destination for :class:`AuditEvent` records."""

    def emit(self, event: AuditEvent) -> None: ...


# --------------------------------------------------------------------------- #
# Pure, deterministic hashing — shared by the writer, the verifier, and any    #
# offline auditor re-checking an NDJSON export.                                #
# --------------------------------------------------------------------------- #


def canonical_json(value: Any) -> str:
    """Stable JSON: sorted keys, no whitespace, ``str()`` fallback for extras."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def compute_payload_hash(redacted_payload: Any) -> str:
    """SHA-256 hex over the canonical JSON of the (already redacted) payload."""
    return hashlib.sha256(canonical_json(redacted_payload).encode("utf-8")).hexdigest()


def _canonical_timestamp(value: datetime) -> str:
    """Dialect-independent timestamp form (naive-UTC ISO, microseconds).

    SQLite returns naive datetimes and Postgres returns aware ones for the same
    stored instant; normalizing to naive UTC keeps write-time and verify-time
    hashes identical across dialects.
    """
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value.isoformat(timespec="microseconds")


def compute_entry_hash(
    *,
    prev_hash: str,
    workspace_id: UUID,
    seq: int,
    occurred_at: datetime,
    actor_type: str,
    actor_id: UUID | None,
    actor_label: str | None,
    action: str,
    target_type: str | None,
    target_id: UUID | None,
    scope_type: str | None,
    scope_id: UUID | None,
    result: str,
    payload_hash: str,
) -> str:
    """SHA-256 hex over the canonical entry tuple (links via ``prev_hash``).

    Field set matches the persisted foundation row shape (deviation from the
    slice doc's ``resource_type``/``outcome`` naming, noted in the module doc).
    """
    tuple_ = [
        prev_hash,
        str(workspace_id),
        seq,
        _canonical_timestamp(occurred_at),
        actor_type,
        str(actor_id) if actor_id is not None else "",
        actor_label or "",
        action,
        target_type or "",
        str(target_id) if target_id is not None else "",
        scope_type or "",
        str(scope_id) if scope_id is not None else "",
        result,
        payload_hash,
    ]
    return hashlib.sha256(canonical_json(tuple_).encode("utf-8")).hexdigest()
