"""``typing.Protocol`` interfaces for every Forge package's public API.

These signatures are **FROZEN** (plan Task 0.3): Phase 1 implementations and the
API/worker layer build against them verbatim. All protocols are
``runtime_checkable`` so tests and DI wiring can assert structural conformance.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol, runtime_checkable

from forge_contracts.constants import RRF_K
from forge_contracts.dtos import (
    AgentObjective,
    AgentRunResult,
    ApprovalRequest,
    BoardFilter,
    BulkUpdate,
    Chunk,
    CIStatus,
    Constitution,
    Decision,
    EpicDTO,
    ExternalTask,
    ForgeTask,
    HealthResult,
    IncidentDTO,
    IndexResult,
    KnowledgeScope,
    MCPConnection,
    MCPResource,
    MCPResourceContent,
    MCPToolResult,
    MilestoneDTO,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    Policy,
    PullRequest,
    PullRequestRequest,
    Ranked,
    RepositoryConnection,
    RepoSyncResult,
    Requirement,
    RerankResult,
    RetrievedChunk,
    SkillProfile,
    SlackDeliveryResult,
    SlackMessage,
    SpecManifest,
    SprintDTO,
    TaskDTO,
    ToolCall,
    ValidationReport,
    WebhookEvent,
    WorkflowDefinition,
    WorkflowRun,
)
from forge_contracts.enums import (
    Direction,
    TaskStatus,
    WorkflowState,
)

# ``WorkflowEngine.transition`` returns a workflow state; the plan names it ``State``.
State = WorkflowState


# --------------------------------------------------------------------------- #
# Knowledge / retrieval                                                        #
# --------------------------------------------------------------------------- #


@runtime_checkable
class KnowledgeStore(Protocol):
    """Index + hybrid-search facade over a knowledge source."""

    def index(self, source_id: str, chunks: list[Chunk]) -> IndexResult: ...

    def search(self, query: str, scope: KnowledgeScope, k: int = 10) -> list[RetrievedChunk]: ...


@runtime_checkable
class Retriever(Protocol):
    """Hybrid retrieval primitives: semantic + keyword + RRF fusion + rerank."""

    def semantic(self, query: str, scope: KnowledgeScope, k: int) -> list[Ranked]: ...

    def keyword(self, query: str, scope: KnowledgeScope, k: int) -> list[Ranked]: ...

    def fuse(self, rankings: list[list[Ranked]], k: int = RRF_K) -> list[Ranked]: ...

    def rerank(self, query: str, candidates: list[Ranked], top_n: int) -> list[RetrievedChunk]: ...


# --------------------------------------------------------------------------- #
# Board                                                                        #
# --------------------------------------------------------------------------- #


@runtime_checkable
class BoardService(Protocol):
    """CRUD + workflow rules for Epic / Task / Sprint / Milestone / Incident."""

    # Epic
    def create_epic(self, data: EpicDTO) -> EpicDTO: ...
    def get_epic(self, epic_id: uuid.UUID) -> EpicDTO: ...
    def update_epic(self, epic_id: uuid.UUID, data: EpicDTO) -> EpicDTO: ...
    def list_epics(self, filter: BoardFilter | None = None) -> list[EpicDTO]: ...
    def delete_epic(self, epic_id: uuid.UUID) -> None: ...

    # Task
    def create_task(self, data: TaskDTO) -> TaskDTO: ...
    def get_task(self, task_id: uuid.UUID) -> TaskDTO: ...
    def update_task(self, task_id: uuid.UUID, data: TaskDTO) -> TaskDTO: ...
    def list_tasks(self, filter: BoardFilter | None = None) -> list[TaskDTO]: ...
    def delete_task(self, task_id: uuid.UUID) -> None: ...

    # Sprint
    def create_sprint(self, data: SprintDTO) -> SprintDTO: ...
    def get_sprint(self, sprint_id: uuid.UUID) -> SprintDTO: ...
    def update_sprint(self, sprint_id: uuid.UUID, data: SprintDTO) -> SprintDTO: ...
    def list_sprints(self, filter: BoardFilter | None = None) -> list[SprintDTO]: ...
    def delete_sprint(self, sprint_id: uuid.UUID) -> None: ...

    # Milestone
    def create_milestone(self, data: MilestoneDTO) -> MilestoneDTO: ...
    def get_milestone(self, milestone_id: uuid.UUID) -> MilestoneDTO: ...
    def update_milestone(self, milestone_id: uuid.UUID, data: MilestoneDTO) -> MilestoneDTO: ...
    def list_milestones(self, filter: BoardFilter | None = None) -> list[MilestoneDTO]: ...
    def delete_milestone(self, milestone_id: uuid.UUID) -> None: ...

    # Incident
    def create_incident(self, data: IncidentDTO) -> IncidentDTO: ...
    def get_incident(self, incident_id: uuid.UUID) -> IncidentDTO: ...
    def update_incident(self, incident_id: uuid.UUID, data: IncidentDTO) -> IncidentDTO: ...
    def list_incidents(self, filter: BoardFilter | None = None) -> list[IncidentDTO]: ...
    def delete_incident(self, incident_id: uuid.UUID) -> None: ...

    # Cross-cutting
    def set_status(self, task_id: uuid.UUID, status: TaskStatus) -> TaskDTO: ...
    def bulk_update(self, updates: list[BulkUpdate]) -> list[TaskDTO]: ...

    def dependency_add(self, task_id: uuid.UUID, depends_on_id: uuid.UUID) -> None:
        """Add a blocks/blocked-by edge; raises ``CycleError`` if it forms a cycle."""
        ...


# --------------------------------------------------------------------------- #
# Spec engine                                                                  #
# --------------------------------------------------------------------------- #


@runtime_checkable
class SpecEngine(Protocol):
    """SDD lifecycle + manifest read/write (spec: SDD Lifecycle)."""

    def constitution_init(
        self, project_id: uuid.UUID, principles: list[str] | None = None
    ) -> Constitution: ...

    def spec_create(
        self, epic_id: uuid.UUID, name: str, requirements: list[Requirement] | None = None
    ) -> SpecManifest: ...

    def spec_clarify(self, spec_id: uuid.UUID) -> SpecManifest: ...

    def spec_plan(self, spec_id: uuid.UUID) -> SpecManifest: ...

    def spec_tasks(self, spec_id: uuid.UUID) -> list[TaskDTO]: ...

    def validate(self, task_id: uuid.UUID) -> ValidationReport: ...

    def read_manifest(self, spec_id: uuid.UUID) -> SpecManifest: ...

    def write_manifest(self, manifest: SpecManifest) -> SpecManifest: ...


# --------------------------------------------------------------------------- #
# Agent runtime                                                                #
# --------------------------------------------------------------------------- #


@runtime_checkable
class AgentRuntime(Protocol):
    """LangGraph single-agent loop: plan -> act -> observe."""

    def run(self, objective: AgentObjective) -> AgentRunResult: ...


# --------------------------------------------------------------------------- #
# Workflow engine                                                              #
# --------------------------------------------------------------------------- #


@runtime_checkable
class WorkflowEngine(Protocol):
    """Postgres-backed FSM for a task's lifecycle."""

    def start(self, task_id: uuid.UUID) -> WorkflowRun: ...

    def transition(self, run_id: uuid.UUID, event: str) -> State: ...

    def load_definition(self, source: str | Path) -> WorkflowDefinition: ...


# --------------------------------------------------------------------------- #
# Policy                                                                       #
# --------------------------------------------------------------------------- #


@runtime_checkable
class PolicyEvaluator(Protocol):
    """Load ``.forge/policy.yaml`` and evaluate tool calls against it."""

    def load(self, repo_root: str | Path) -> Policy: ...

    def evaluate(self, action: ToolCall, policy: Policy) -> Decision: ...


# --------------------------------------------------------------------------- #
# Skill profiles                                                               #
# --------------------------------------------------------------------------- #


@runtime_checkable
class SkillProfileRegistry(Protocol):
    """Resolve skill profiles and inject their behaviour into an objective."""

    def get(self, name: str) -> SkillProfile:
        """Resolve a profile by name; raises ``UnknownSkillProfileError`` if absent."""
        ...

    def inject(self, profile: SkillProfile, context: AgentObjective) -> AgentObjective: ...


# --------------------------------------------------------------------------- #
# MCP                                                                          #
# --------------------------------------------------------------------------- #


@runtime_checkable
class MCPClient(Protocol):
    """MCP client: audited, read-only by default (spec: MCP Security Rules)."""

    def connect(self, conn: MCPConnection) -> None: ...

    def list_resources(self, namespace: str | None = None) -> list[MCPResource]: ...

    def read_resource(self, uri: str) -> MCPResourceContent: ...

    def call_tool(self, name: str, arguments: dict[str, object]) -> MCPToolResult: ...


# --------------------------------------------------------------------------- #
# Integrations                                                                 #
# --------------------------------------------------------------------------- #


@runtime_checkable
class PMAdapter(Protocol):
    """External project-management adapter (spec: External PM Adapter Contract)."""

    def sync_in(self, external_task: ExternalTask) -> ForgeTask: ...

    def sync_out(self, forge_task: ForgeTask) -> ExternalTask: ...

    def subscribe(self, webhook_event: WebhookEvent) -> None: ...

    def map_status(self, external: str, direction: Direction) -> str: ...

    def map_priority(self, external: str, direction: Direction) -> str: ...

    def map_fields(
        self, external: dict[str, object], direction: Direction
    ) -> dict[str, object]: ...

    def get_connection_health(self) -> HealthResult: ...


@runtime_checkable
class IntegrationClient(Protocol):
    """Common surface for outbound integration clients."""

    def health(self) -> HealthResult: ...


@runtime_checkable
class GitHubClient(Protocol):
    """GitHub App client (spec: V1 GitHub integration). Fixture-backed for tests."""

    def sync_repo(self, connection: RepositoryConnection) -> RepoSyncResult: ...

    def open_pr(self, request: PullRequestRequest) -> PullRequest: ...

    def request_reviews(self, pr: PullRequest, reviewers: list[str]) -> None: ...

    def parse_webhook(self, event: WebhookEvent) -> CIStatus: ...


@runtime_checkable
class SlackNotifier(Protocol):
    """Slack notifier (spec: V1 Slack integration)."""

    def notify(self, message: SlackMessage) -> SlackDeliveryResult: ...

    def notify_approval(self, request: ApprovalRequest) -> SlackDeliveryResult: ...


# --------------------------------------------------------------------------- #
# Model / embedding / reranker clients (BYOK, provider-agnostic)               #
# --------------------------------------------------------------------------- #


@runtime_checkable
class EmbeddingClient(Protocol):
    """Provider-agnostic embedding client (BYOK; deterministic fake for tests)."""

    @property
    def dimension(self) -> int: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


@runtime_checkable
class RerankerClient(Protocol):
    """Cross-encoder reranker client (e.g. Jina v2; fixture-backed fake for tests)."""

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[RerankResult]: ...


@runtime_checkable
class ModelClient(Protocol):
    """Provider-agnostic chat/completion client (BYOK; fake for tests)."""

    def complete(self, request: ModelRequest) -> ModelResponse: ...

    def stream(self, request: ModelRequest) -> Iterator[ModelStreamEvent]: ...


__all__ = [
    "AgentRuntime",
    "BoardService",
    "EmbeddingClient",
    "GitHubClient",
    "IntegrationClient",
    "KnowledgeStore",
    "MCPClient",
    "ModelClient",
    "PMAdapter",
    "PolicyEvaluator",
    "RerankerClient",
    "Retriever",
    "SkillProfileRegistry",
    "SlackNotifier",
    "SpecEngine",
    "State",
    "WorkflowEngine",
]
