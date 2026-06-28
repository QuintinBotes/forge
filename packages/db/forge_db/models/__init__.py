"""Forge SQLAlchemy models — the shared data-model substrate.

Importing this package registers every model on ``Base.metadata`` (used by
Alembic's ``env.py`` and by ``Base.metadata.create_all`` in tests).
"""

from __future__ import annotations

from forge_db.models import enums
from forge_db.models.audit import AuditLog
from forge_db.models.automation import AutomationExecution, AutomationRule
from forge_db.models.connections import MCPConnection, RepositoryConnection
from forge_db.models.enums import (
    AccessLevel,
    APIKeyKind,
    ApprovalGate,
    ApprovalStatus,
    AutomationActionType,
    AutomationEntityType,
    AutomationExecutionStatus,
    AutomationTriggerSource,
    AutomationTriggerType,
    ChunkType,
    EngineBackend,
    ExecutionMode,
    IncidentSeverity,
    IncidentState,
    KnowledgeSourceKind,
    MCPAuthType,
    MCPIndexStrategy,
    MCPTransport,
    PMAuthType,
    PMConflictPolicy,
    PMConnectionStatus,
    PMDeliveryStatus,
    PMProvider,
    PMSyncDirection,
    PMSyncState,
    PRGroupStatus,
    PrincipalType,
    Priority,
    PRMergeState,
    ProjectVisibility,
    RepoProvider,
    RepoRole,
    RunStatus,
    SandboxKind,
    SandboxNetwork,
    SandboxStatus,
    ScopeType,
    SpecStatus,
    SyncMode,
    TaskKind,
    TaskStatus,
    TeamRole,
    UserRole,
    WorkflowState,
)
from forge_db.models.incidents import (
    IncidentAlert,
    IncidentEvent,
    Postmortem,
    PostmortemActionItem,
    RemediationPlan,
)
from forge_db.models.knowledge import (
    CHUNK_TYPE_WEIGHTS,
    EMBEDDING_DIM,
    KnowledgeSource,
    RetrievalChunk,
)
from forge_db.models.mcp_index import (
    KnowledgeSyncRun,
    MCPIndexedResource,
)
from forge_db.models.multi_repo import AgentRepoWorkspace, PRGroup
from forge_db.models.planning import (
    Epic,
    Incident,
    Milestone,
    SpecDocument,
    Sprint,
    Task,
)
from forge_db.models.pm import PMConnection, PMTaskLink, PMWebhookDelivery
from forge_db.models.policy_rule_evaluation import PolicyRuleEvaluation
from forge_db.models.profiles import PolicyProfile, SkillProfile
from forge_db.models.project import Constitution, Project
from forge_db.models.project_team_access import ProjectTeamAccess
from forge_db.models.role_grant import RoleGrant
from forge_db.models.runs import AgentRun, ApprovalRequest, SubAgentRun, WorkflowRun
from forge_db.models.sandbox import SandboxInstance
from forge_db.models.sprint_velocity import (
    SprintBurndownSnapshot,
    SprintScopeEvent,
    SprintVelocity,
)
from forge_db.models.team import Team
from forge_db.models.team_member import TeamMember
from forge_db.models.workflow_editor import (
    RevisionStatus,
    RevisionValidationStatus,
    WorkflowDefinition,
    WorkflowDefinitionRevision,
    WorkflowDefinitionSource,
)
from forge_db.models.workspace import APIKey, User, Workspace

__all__ = [
    "CHUNK_TYPE_WEIGHTS",
    "EMBEDDING_DIM",
    "APIKey",
    "APIKeyKind",
    "AccessLevel",
    "AgentRepoWorkspace",
    "AgentRun",
    "ApprovalGate",
    "ApprovalRequest",
    "ApprovalStatus",
    "AuditLog",
    "AutomationActionType",
    "AutomationEntityType",
    "AutomationExecution",
    "AutomationExecutionStatus",
    "AutomationRule",
    "AutomationTriggerSource",
    "AutomationTriggerType",
    "ChunkType",
    "Constitution",
    "EngineBackend",
    "Epic",
    "ExecutionMode",
    "Incident",
    "IncidentAlert",
    "IncidentEvent",
    "IncidentSeverity",
    "IncidentState",
    "KnowledgeSource",
    "KnowledgeSourceKind",
    "KnowledgeSyncRun",
    "MCPAuthType",
    "MCPConnection",
    "MCPIndexStrategy",
    "MCPIndexedResource",
    "MCPTransport",
    "Milestone",
    "PMAuthType",
    "PMConflictPolicy",
    "PMConnection",
    "PMConnectionStatus",
    "PMDeliveryStatus",
    "PMProvider",
    "PMSyncDirection",
    "PMSyncState",
    "PMTaskLink",
    "PMWebhookDelivery",
    "PRGroup",
    "PRGroupStatus",
    "PRMergeState",
    "PolicyProfile",
    "PolicyRuleEvaluation",
    "Postmortem",
    "PostmortemActionItem",
    "PrincipalType",
    "Priority",
    "Project",
    "ProjectTeamAccess",
    "ProjectVisibility",
    "RemediationPlan",
    "RepoProvider",
    "RepoRole",
    "RepositoryConnection",
    "RetrievalChunk",
    "RevisionStatus",
    "RevisionValidationStatus",
    "RoleGrant",
    "RunStatus",
    "SandboxInstance",
    "SandboxKind",
    "SandboxNetwork",
    "SandboxStatus",
    "ScopeType",
    "SkillProfile",
    "SpecDocument",
    "SpecStatus",
    "Sprint",
    "SprintBurndownSnapshot",
    "SprintScopeEvent",
    "SprintVelocity",
    "SubAgentRun",
    "SyncMode",
    "Task",
    "TaskKind",
    "TaskStatus",
    "Team",
    "TeamMember",
    "TeamRole",
    "User",
    "UserRole",
    "WorkflowDefinition",
    "WorkflowDefinitionRevision",
    "WorkflowDefinitionSource",
    "WorkflowRun",
    "WorkflowState",
    "Workspace",
    "enums",
]
