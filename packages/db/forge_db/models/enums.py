"""Enumerations for the Forge data model.

Values mirror ``docs/FORGE_SPEC.md`` exactly (roles, kinds, statuses, workflow
states). All are ``str`` enums so they serialize cleanly and store as VARCHAR
(SQLAlchemy ``Enum(native_enum=False)``) for cross-dialect portability.
"""

from __future__ import annotations

import enum

# F21 automation enums are defined once in the frozen contracts package and
# re-exported here so the SQLAlchemy column types (enum_type(...)) and the rest
# of forge_db follow the local ``forge_db.models.enums`` import convention.
from forge_contracts.automation import (
    AutomationActionType,
    AutomationEntityType,
    AutomationExecutionStatus,
    AutomationTriggerSource,
    AutomationTriggerType,
)


class UserRole(enum.StrEnum):
    """Workspace member roles (spec: admin, member, viewer, agent-runner)."""

    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"
    AGENT_RUNNER = "agent-runner"


class APIKeyKind(enum.StrEnum):
    """BYOK key categories."""

    MODEL_PROVIDER = "model_provider"
    INTEGRATION_TOKEN = "integration_token"
    MCP_TOKEN = "mcp_token"
    SYSTEM = "system"


class RepoProvider(enum.StrEnum):
    GITHUB = "github"
    GITLAB = "gitlab"
    BITBUCKET = "bitbucket"


class MCPTransport(enum.StrEnum):
    HTTP = "http"
    STDIO = "stdio"
    SSE = "sse"


class MCPAuthType(enum.StrEnum):
    OAUTH = "oauth"
    API_KEY = "api_key"
    NONE = "none"


class MCPIndexStrategy(enum.StrEnum):
    SYNC_AND_INDEX = "sync_and_index"
    QUERY_THROUGH = "query_through"


class SyncMode(enum.StrEnum):
    """Knowledge sync modes (spec: Knowledge Sync Modes table)."""

    FULL = "full"
    INCREMENTAL = "incremental"
    ON_DEMAND = "on_demand"
    QUERY_THROUGH = "query_through"
    SYNC_AND_INDEX = "sync_and_index"


class KnowledgeSourceKind(enum.StrEnum):
    REPO = "repo"
    MCP = "mcp"
    DOCUMENT = "document"
    URL = "url"


class ChunkType(enum.StrEnum):
    """Retrieval chunk source types (spec: Chunk Types and Priority Weights)."""

    MARKDOWN = "markdown"
    CODE = "code"
    SUMMARY = "summary"
    README = "readme"
    POLICY = "policy"
    SPEC = "spec"
    MCP_RESOURCE = "mcp_resource"


class TaskKind(enum.StrEnum):
    """Task kinds (spec: Task Schema ``kind``)."""

    FEATURE = "feature"
    BUG = "bug"
    CHORE = "chore"
    SPIKE = "spike"
    INCIDENT = "incident"
    CHANGE_REQUEST = "change_request"
    DOC = "doc"


class TaskStatus(enum.StrEnum):
    BACKLOG = "backlog"
    READY = "ready"
    READY_FOR_AGENT = "ready_for_agent"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELLED = "cancelled"


class Priority(enum.StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class ExecutionMode(enum.StrEnum):
    """Spec: ``execution_mode`` (single_agent | supervised_multi_agent)."""

    SINGLE_AGENT = "single_agent"
    SUPERVISED_MULTI_AGENT = "supervised_multi_agent"


class SpecStatus(enum.StrEnum):
    """Spec manifest status (spec: Spec Manifest Schema)."""

    DRAFT = "draft"
    CLARIFYING = "clarifying"
    APPROVED = "approved"
    IMPLEMENTING = "implementing"
    VALIDATED = "validated"
    CLOSED = "closed"


class WorkflowState(enum.StrEnum):
    """Default feature workflow states (spec: Default Feature Workflow States)."""

    CREATED = "created"
    SPEC_DRAFTING = "spec_drafting"
    CLARIFICATION = "clarification"
    SPEC_REVIEW = "spec_review"
    SPEC_APPROVED = "spec_approved"
    PLAN_DRAFTING = "plan_drafting"
    PLAN_REVIEW = "plan_review"
    TASK_GENERATION = "task_generation"
    TASK_READY = "task_ready"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    PR_OPENED = "pr_opened"
    AWAITING_REVIEW = "awaiting_review"
    MERGED = "merged"
    CLOSED = "closed"
    # Error paths
    NEEDS_HUMAN_INPUT = "needs_human_input"
    FAILED = "failed"
    CANCELLED = "cancelled"


class IncidentSeverity(enum.StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class IncidentState(enum.StrEnum):
    """Incident workflow states (spec: Incident Workflow States)."""

    ALERT_RECEIVED = "alert_received"
    INCIDENT_CREATED = "incident_created"
    CONTEXT_GATHERING = "context_gathering"
    IMPACT_ASSESSED = "impact_assessed"
    REMEDIATION_PROPOSED = "remediation_proposed"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING_RUNBOOK = "executing_runbook"
    MONITORING = "monitoring"
    RESOLVED = "resolved"
    POSTMORTEM_CREATED = "postmortem_created"


class RunStatus(enum.StrEnum):
    """Status for workflow/agent/sub-agent runs."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ESCALATED = "escalated"
    CANCELLED = "cancelled"


class SandboxKind(enum.StrEnum):
    """Command-execution isolation provider (F19; spec: Sandbox V1/V2)."""

    WORKTREE = "worktree"
    CONTAINER = "container"


class SandboxNetwork(enum.StrEnum):
    """Container network posture (F19)."""

    NONE = "none"
    EGRESS = "egress"


class SandboxStatus(enum.StrEnum):
    """Lifecycle of a sandbox instance (F19)."""

    CREATING = "creating"
    RUNNING = "running"
    EXITED = "exited"
    REMOVED = "removed"
    FAILED = "failed"


class ApprovalGate(enum.StrEnum):
    """Approval gate types (spec: Approval Gate Types)."""

    SPEC = "spec"
    PLAN = "plan"
    PR = "pr"
    DEPLOY = "deploy"
    INCIDENT_REMEDIATION = "incident_remediation"
    POLICY_OVERRIDE = "policy_override"


class ApprovalStatus(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"


# --------------------------------------------------------------------------- #
# F18 — external PM adapters (Jira, Linear)                                    #
# --------------------------------------------------------------------------- #


class PMProvider(enum.StrEnum):
    """Supported external project-management providers (F18)."""

    JIRA = "jira"
    LINEAR = "linear"


class PMAuthType(enum.StrEnum):
    OAUTH = "oauth"
    API_TOKEN = "api_token"


class PMSyncDirection(enum.StrEnum):
    BIDIRECTIONAL = "bidirectional"
    INBOUND_ONLY = "inbound_only"
    OUTBOUND_ONLY = "outbound_only"


class PMConflictPolicy(enum.StrEnum):
    FORGE_WINS = "forge_wins"
    EXTERNAL_WINS = "external_wins"
    NEWEST_WINS = "newest_wins"
    MANUAL = "manual"


class PMConnectionStatus(enum.StrEnum):
    PENDING = "pending"
    CONNECTED = "connected"
    ERROR = "error"
    DISABLED = "disabled"


class PMSyncState(enum.StrEnum):
    SYNCED = "synced"
    PENDING_OUT = "pending_out"
    PENDING_IN = "pending_in"
    CONFLICT = "conflict"
    ERROR = "error"


class PMDeliveryStatus(enum.StrEnum):
    RECEIVED = "received"
    PROCESSED = "processed"
    SKIPPED = "skipped"
    ECHO_SUPPRESSED = "echo_suppressed"
    ERROR = "error"


class RepoRole(enum.StrEnum):
    """F22 multi-repo: a repo target's coordination role within a task."""

    PRIMARY = "primary"
    SECONDARY = "secondary"


class PRGroupStatus(enum.StrEnum):
    """F22 multi-repo PR-group (merge-unit) status."""

    OPEN = "open"
    READY = "ready"
    MERGING = "merging"
    MERGED = "merged"
    PARTIALLY_MERGED = "partially_merged"
    FAILED = "failed"


class PRMergeState(enum.StrEnum):
    """F22 per-repo PR merge state within a group."""

    PENDING = "pending"
    MERGED = "merged"
    SKIPPED = "skipped"
    FAILED = "failed"


__all__ = [
    "APIKeyKind",
    "ApprovalGate",
    "ApprovalStatus",
    "AutomationActionType",
    "AutomationEntityType",
    "AutomationExecutionStatus",
    "AutomationTriggerSource",
    "AutomationTriggerType",
    "ChunkType",
    "ExecutionMode",
    "IncidentSeverity",
    "IncidentState",
    "KnowledgeSourceKind",
    "MCPAuthType",
    "MCPIndexStrategy",
    "MCPTransport",
    "PMAuthType",
    "PMConflictPolicy",
    "PMConnectionStatus",
    "PMDeliveryStatus",
    "PMProvider",
    "PMSyncDirection",
    "PMSyncState",
    "PRGroupStatus",
    "PRMergeState",
    "Priority",
    "RepoProvider",
    "RepoRole",
    "RunStatus",
    "SandboxKind",
    "SandboxNetwork",
    "SandboxStatus",
    "SpecStatus",
    "SyncMode",
    "TaskKind",
    "TaskStatus",
    "UserRole",
    "WorkflowState",
]
