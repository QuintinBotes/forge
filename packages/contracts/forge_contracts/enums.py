"""Enumerations for the Forge shared contracts.

Values mirror ``docs/FORGE_SPEC.md`` exactly (roles, kinds, statuses, workflow
and incident states, approval gates) and are kept byte-for-byte compatible with
``forge_db.models.enums`` so DTOs and ORM rows interoperate. All are ``str``
enums so they serialise to their wire value and store as plain ``VARCHAR``.

This module is **frozen**: Phase 1 builds against these values.
"""

from __future__ import annotations

import enum


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
    """MCP transports (spec: http preferred, stdio, sse legacy)."""

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
    """Status for workflow / agent / sub-agent runs."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ESCALATED = "escalated"
    CANCELLED = "cancelled"


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


class SubAgentRole(enum.StrEnum):
    """Sub-agent role definitions (spec: Subagent Role Definitions)."""

    PLANNER = "planner"
    RESEARCHER = "researcher"
    IMPLEMENTER = "implementer"
    TESTER = "tester"
    REVIEWER = "reviewer"
    SECURITY = "security"
    #: Heterogeneous red-team adversary (Red-Team Gate): attacks a candidate diff
    #: from a DIFFERENT model/tier than the coder and must produce a failing
    #: executable test or a structured spec-violation to block it. Writes tests,
    #: never product code.
    ADVERSARY = "adversary"


class StepKind(enum.StrEnum):
    """Kinds of agent step recorded in an ``AgentRunResult`` trace."""

    PLAN = "plan"
    TOOL_CALL = "tool_call"
    OBSERVATION = "observation"
    DECISION = "decision"
    MESSAGE = "message"
    OUTPUT = "output"
    ERROR = "error"
    HANDOFF = "handoff"


class DecisionEffect(enum.StrEnum):
    """Result of a policy evaluation."""

    ALLOW = "allow"
    DENY = "deny"
    REQUIRES_APPROVAL = "requires_approval"


class RuleEffect(enum.StrEnum):
    """Effect a conditional policy rule applies (F29 — advanced policy engine).

    A rule may always *tighten* (``deny`` / ``require_approval``); it may only
    *loosen* (``allow``) a non-critical base denial and only with
    ``override_base=True`` (see ``ConditionalRule``).
    """

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class Direction(enum.StrEnum):
    """Mapping direction for the external PM adapter contract."""

    IN = "in"
    OUT = "out"


class PRState(enum.StrEnum):
    """GitHub pull request state."""

    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"
    DRAFT = "draft"


class CIState(enum.StrEnum):
    """CI / check run status parsed from webhooks."""

    PENDING = "pending"
    SUCCESS = "success"
    FAILURE = "failure"
    ERROR = "error"


# --------------------------------------------------------------------------- #
# F26 — sprint management & velocity                                           #
# --------------------------------------------------------------------------- #
#
# Foundation deviation note: the idealized F26 slice assumed an F01 ``sprints``
# table with a native ``sprint_state`` ENUM and a per-project ``task_statuses``
# /``StatusCategory`` model. The real foundation stores ``Sprint.status`` as a
# plain ``VARCHAR`` and ``Task.status`` as the :class:`TaskStatus` enum (no
# status-category table). So ``SprintState`` below is stored as a string in
# ``Sprint.status`` (no enum migration needed) and "done" is derived from
# ``TaskStatus.DONE`` / cancelled from ``TaskStatus.CANCELLED``. See slice notes.


class SprintState(enum.StrEnum):
    """Sprint lifecycle states (stored in ``Sprint.status``)."""

    PLANNED = "planned"
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class SprintScopeEventType(enum.StrEnum):
    """The kinds of append-only scope change recorded for a sprint."""

    SPRINT_STARTED = "sprint_started"  # baseline marker; points_delta=committed
    TASK_ADDED = "task_added"  # +estimate
    TASK_REMOVED = "task_removed"  # -estimate
    TASK_COMPLETED = "task_completed"  # -estimate from remaining
    TASK_REOPENED = "task_reopened"  # +estimate back to remaining
    ESTIMATE_CHANGED = "estimate_changed"  # delta = after - before
    SPRINT_COMPLETED = "sprint_completed"  # finalize marker
    SPRINT_CANCELLED = "sprint_cancelled"


class CarryoverTarget(enum.StrEnum):
    """Where incomplete tasks go when a sprint is completed."""

    BACKLOG = "backlog"
    NEXT_SPRINT = "next_sprint"
    LEAVE = "leave"


class ScopeActorKind(enum.StrEnum):
    """Who triggered a scope event."""

    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"


__all__ = [
    "APIKeyKind",
    "ApprovalGate",
    "ApprovalStatus",
    "CIState",
    "CarryoverTarget",
    "ChunkType",
    "DecisionEffect",
    "Direction",
    "ExecutionMode",
    "IncidentSeverity",
    "IncidentState",
    "KnowledgeSourceKind",
    "MCPAuthType",
    "MCPIndexStrategy",
    "MCPTransport",
    "PRState",
    "Priority",
    "RepoProvider",
    "RunStatus",
    "ScopeActorKind",
    "SpecStatus",
    "SprintScopeEventType",
    "SprintState",
    "StepKind",
    "SubAgentRole",
    "SyncMode",
    "TaskKind",
    "TaskStatus",
    "UserRole",
    "WorkflowState",
]
