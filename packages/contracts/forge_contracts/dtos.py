"""Pydantic v2 DTOs shared across all Forge packages (frozen — Task 0.3).

Definitions are ordered leaf-first so nested models resolve at class-creation
time. Every DTO is a plain transport object: no DB session, no I/O. Ids that
reference ``forge_db`` rows are ``uuid.UUID``; human-facing identifiers (e.g.
``SPEC-17``, ``TASK-123``) are ``str`` ``key`` fields.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from forge_contracts.conditions import ConditionGroup
from forge_contracts.constants import DEFAULT_CONFIDENCE_THRESHOLD, DEFAULT_MAX_RETRIES
from forge_contracts.enums import (
    ApprovalGate,
    ApprovalStatus,
    ChunkType,
    CIState,
    DecisionEffect,
    ExecutionMode,
    IncidentSeverity,
    IncidentState,
    MCPAuthType,
    MCPIndexStrategy,
    MCPTransport,
    Priority,
    PRState,
    RepoProvider,
    RuleEffect,
    RunStatus,
    SpecStatus,
    StepKind,
    SyncMode,
    TaskKind,
    TaskStatus,
)

#: Severity attached to a policy decision / conditional rule (F29).
Severity = Literal["info", "warning", "critical"]


class _Model(BaseModel):
    """Shared base: tolerant of unknown keys, populatable by field name or alias."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


# --------------------------------------------------------------------------- #
# Knowledge / retrieval                                                        #
# --------------------------------------------------------------------------- #


class Chunk(_Model):
    """An un-indexed unit produced by chunking (code AST / markdown paragraph)."""

    content: str
    chunk_type: ChunkType = ChunkType.CODE
    path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    language: str | None = None
    symbol: str | None = None
    content_hash: str | None = None
    weight: float = 1.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievedChunk(_Model):
    """A chunk returned from a search, with score and source attribution."""

    id: str | None = None
    content: str
    chunk_type: ChunkType = ChunkType.CODE
    path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    score: float = 0.0
    rerank_score: float | None = None
    weight: float = 1.0
    source_id: str | None = None
    source_uri: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Ranked(_Model):
    """An intermediate ranking entry used by the hybrid retriever and RRF fusion."""

    chunk_id: str
    score: float
    rank: int
    chunk: RetrievedChunk | None = None


class KnowledgeScope(_Model):
    """Scoping for retrieval (spec: Task Schema ``knowledge_scope``)."""

    workspace_id: uuid.UUID | None = None
    repos: list[str] = Field(default_factory=list)
    mcp_sources: list[str] = Field(default_factory=list)
    source_types: list[str] = Field(default_factory=list)
    freshness_min_hours: int | None = None
    #: F40 MCP ACL: a namespace entitlement allow-list re-checked at *query* time.
    #: When non-empty, an indexed MCP chunk is returned only if its
    #: ``mcp_namespace`` is in this set — so a connection's ``allowed_namespaces``
    #: is re-enforced against the index (indexing entitlement can be revoked
    #: between sync and query, and it blocks cross-tenant namespace leakage).
    #: Chunks with no ``mcp_namespace`` (e.g. repo content) are unaffected.
    namespaces: list[str] = Field(default_factory=list)


class IndexResult(_Model):
    """Outcome of indexing chunks into a knowledge store."""

    source_id: str
    indexed: int = 0
    updated: int = 0
    deleted: int = 0
    skipped: int = 0
    errors: list[str] = Field(default_factory=list)


class RerankResult(_Model):
    """A single reranked document returned by a ``RerankerClient``."""

    index: int
    score: float
    document: str | None = None


# --------------------------------------------------------------------------- #
# Agent runtime                                                                #
# --------------------------------------------------------------------------- #


class ToolCall(_Model):
    """A request to invoke a tool — the unit policy evaluation acts on."""

    tool: str
    action: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    path: str | None = None
    resource: str | None = None
    connection_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConditionalMatch(_Model):
    """One conditional rule that fired during evaluation (F29).

    Recorded on :class:`Decision.conditional_matches` and surfaced in the F36
    approval UI's "Risks flagged" panel.
    """

    rule_id: str
    effect: RuleEffect
    severity: Severity = "warning"
    reason: str


class Decision(_Model):
    """The result of evaluating a ``ToolCall`` against a ``Policy``.

    F29 adds three additive, default-empty fields (the flat F04 shape is
    otherwise unchanged): ``severity`` classifies the decision (the ``critical``
    floor can never be loosened by a conditional rule), ``conditional_matches``
    records every conditional rule that fired, and ``base_effect`` records the
    flat F04 effect before the conditional layer composed on top (``None`` when
    no conditional rule contributed — i.e. the pure F04 decision).
    """

    effect: DecisionEffect = DecisionEffect.DENY
    reason: str | None = None
    matched_rule: str | None = None
    requires_approval: bool = False
    approval_gate: ApprovalGate | None = None
    # --- F29 conditional layer (additive) ---------------------------------- #
    severity: Severity = "info"
    conditional_matches: list[ConditionalMatch] = Field(default_factory=list)
    base_effect: DecisionEffect | None = None

    @property
    def allowed(self) -> bool:
        """True when the action may proceed without being blocked."""
        return self.effect is DecisionEffect.ALLOW


class Step(_Model):
    """One step in an agent run trace (plan / tool call / observation / ...)."""

    index: int | None = None
    kind: StepKind = StepKind.MESSAGE
    thought: str | None = None
    tool_call: ToolCall | None = None
    observation: str | None = None
    output: str | None = None
    decision: Decision | None = None
    confidence: float | None = None
    duration_ms: int | None = None
    timestamp: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AcceptanceCriterion(_Model):
    """An acceptance criterion (spec / task schema). ``req_refs`` link requirements."""

    id: str
    text: str
    req_refs: list[str] = Field(default_factory=list)
    spec_ref: str | None = None


class RepoTarget(_Model):
    """A repository an agent run may write to (spec: Task Schema ``repo_targets``).

    F22 widens this with multi-repo coordination fields. They are all optional
    with V1-compatible defaults so single-repo tasks (the V1 shape) keep parsing
    and behaving identically; ``role`` only becomes load-bearing when a task has
    more than one repo target (see ``MergePlanBuilder``).
    """

    repo: str
    branch_strategy: str = "task_branch"
    branch_prefix: str | None = None
    base_branch: str = "main"
    worktree: bool = True
    # --- F22 multi-repo coordination ---------------------------------------- #
    #: Exactly one target in a multi-repo task is ``primary`` (the dependency
    #: root / merge anchor); the rest are ``secondary``.
    role: Literal["primary", "secondary"] = "secondary"
    #: ``repo`` ids whose PRs must merge BEFORE this repo's PR (a DAG edge).
    depends_on: list[str] = Field(default_factory=list)
    #: Per-repo skill-profile override; ``None`` falls back to the task default.
    skill_profile: str | None = None
    #: When ``False`` an empty/absent diff for this repo does not block the merge
    #: gate (e.g. a generated-client repo that turned out to need no change).
    required_for_merge: bool = True


class ApprovalPolicy(_Model):
    """Per-task approval gates (spec: ``requires_approval``)."""

    spec: bool = False
    plan: bool = False
    pr: bool = True
    deploy: bool = True


class SubAgentPolicy(_Model):
    """Sub-agent permission envelope (spec: ``subagent_policy`` / ``subagent_rules``)."""

    allowed: bool = False
    allowed_roles: list[str] = Field(default_factory=list)
    max_parallel: int = 0


class HandoffRules(_Model):
    """Escalation / handoff triggers (spec: Task Schema ``handoff_rules``)."""

    confidence_below: float = DEFAULT_CONFIDENCE_THRESHOLD
    on_test_failure_after_retries: int = 2
    on_policy_conflict: str = "escalate"
    on_missing_spec_approval: str = "block"


# --------------------------------------------------------------------------- #
# Skill profiles                                                               #
# --------------------------------------------------------------------------- #


class SkillProfile(_Model):
    """A behaviour profile injected into an agent objective (spec: Skill Profiles).

    Fields are the union of every profile in the spec so any profile parses.
    """

    name: str
    description: str | None = None
    requires_plan: bool = False
    requires_tests_before_implementation: bool = False
    min_test_coverage: int | None = None
    verification_steps: list[str] = Field(default_factory=list)
    review_required: bool = False
    forbidden_shortcuts: list[str] = Field(default_factory=list)
    accessibility_check: bool = False
    requires_human_approval_before_action: bool = False
    human_review_required: bool = False
    max_blast_radius: str | None = None
    allowed_actions: list[str] = Field(default_factory=list)
    forbidden_actions: list[str] = Field(default_factory=list)
    output_type: str | None = None
    tools: list[str] = Field(default_factory=list)
    report_format: str | None = None


# --------------------------------------------------------------------------- #
# Agent objective / result                                                     #
# --------------------------------------------------------------------------- #


class AgentObjective(_Model):
    """Structured input to ``AgentRuntime.run`` (spec: AgentRun ``inputs``)."""

    task_id: uuid.UUID | None = None
    key: str | None = None
    objective: str
    description: str | None = None
    instructions: str | None = None
    execution_mode: ExecutionMode = ExecutionMode.SINGLE_AGENT
    skill_profile: SkillProfile | None = None
    repo_targets: list[RepoTarget] = Field(default_factory=list)
    knowledge_scope: KnowledgeScope | None = None
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    allowed_actions: list[str] = Field(default_factory=list)
    restricted_actions: list[str] = Field(default_factory=list)
    requires_approval: ApprovalPolicy = Field(default_factory=ApprovalPolicy)
    subagent_policy: SubAgentPolicy = Field(default_factory=SubAgentPolicy)
    handoff_rules: HandoffRules = Field(default_factory=HandoffRules)
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
    max_retries: int = DEFAULT_MAX_RETRIES
    model: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)

    @property
    def primary_repo_target(self) -> RepoTarget | None:
        """The single ``role='primary'`` target, else the first target (V1).

        For a single-repo task (the V1 shape) the lone target is the primary
        regardless of its declared ``role`` — this keeps ``objective.repo_target``
        style access working byte-for-byte for V1 callers.
        """
        if not self.repo_targets:
            return None
        for target in self.repo_targets:
            if target.role == "primary":
                return target
        return self.repo_targets[0]

    @property
    def repo_target(self) -> RepoTarget | None:
        """V1 back-compat alias for :attr:`primary_repo_target`."""
        return self.primary_repo_target


class RepoChangeSet(_Model):
    """The diff an agent produced in one repo worktree (F22 multi-repo result).

    ``has_changes=False`` means the agent decided this repo needed no edit; no PR
    is opened for it and it is excluded from the merge gate.
    """

    repo: str
    branch_name: str | None = None
    base_commit_sha: str | None = None
    head_commit_sha: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    diff_stat: dict[str, int] = Field(default_factory=dict)
    has_changes: bool = False


class AgentRunResult(_Model):
    """Structured output of ``AgentRuntime.run`` (spec: AgentRun ``steps``/output)."""

    run_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    status: RunStatus = RunStatus.PENDING
    steps: list[Step] = Field(default_factory=list)
    output: str | None = None
    summary: str | None = None
    confidence: float | None = None
    needs_human: bool = False
    acceptance_criteria_satisfied: list[str] = Field(default_factory=list)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    # F22: one change set per repo target (empty on the V1 single-repo path).
    repo_change_sets: list[RepoChangeSet] = Field(default_factory=list)

    @property
    def changed_files(self) -> list[str]:
        """V1 back-compat: the primary (first) repo's changed files, flattened."""
        if not self.repo_change_sets:
            return []
        return list(self.repo_change_sets[0].changed_files)


# --------------------------------------------------------------------------- #
# Multi-repo execution (F22)                                                   #
# --------------------------------------------------------------------------- #


class MergePlan(_Model):
    """The dependency-ordered merge plan for a multi-repo run (F22).

    ``merge_order`` is a topological sort of the ``depends_on`` DAG (a repo's
    dependencies merge before it); the ``primary`` repo sorts first among repos
    with no outstanding dependency.
    """

    primary_repo_id: str
    merge_order: list[str] = Field(default_factory=list)
    edges: dict[str, list[str]] = Field(default_factory=dict)


class CrossPRLink(_Model):
    """One repo's PR within a multi-repo PR group (for cross-PR link blocks)."""

    repo_id: str
    pr_number: int | None = None
    url: str | None = None
    merge_order: int = 0


class PRGroup(_Model):
    """The set of PRs opened by one multi-repo run — the merge unit (F22)."""

    id: uuid.UUID | None = None
    workflow_run_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    merge_order: list[str] = Field(default_factory=list)
    prs: list[CrossPRLink] = Field(default_factory=list)
    status: Literal["open", "ready", "merging", "merged", "partially_merged", "failed"] = "open"
    merged_repo_ids: list[str] = Field(default_factory=list)


class RepoMergeStatus(_Model):
    """Per-repo mergeability within the aggregate gate (F22)."""

    repo_id: str
    has_changes: bool = True
    required_for_merge: bool = True
    review_approved: bool = False
    ci_green: bool = False
    spec_validated: bool = False
    merge_order: int = 0
    blocking_reasons: list[str] = Field(default_factory=list)


class MultiRepoMergeGateResult(_Model):
    """Aggregate merge gate across every required, changed repo (F22)."""

    can_merge: bool = False
    repos: list[RepoMergeStatus] = Field(default_factory=list)
    merge_order: list[str] = Field(default_factory=list)
    blocking_reasons: list[str] = Field(default_factory=list)


class MergeGroupOutcome(_Model):
    """The result of an ordered, halt-on-failure group merge (F22)."""

    status: Literal["merged", "partially_merged", "blocked", "failed"] = "blocked"
    merged_repo_ids: list[str] = Field(default_factory=list)
    failed_repo_id: str | None = None
    workflow_state: str | None = None
    gate: MultiRepoMergeGateResult | None = None


# --------------------------------------------------------------------------- #
# Policy (.forge/policy.yaml)                                                  #
# --------------------------------------------------------------------------- #


class WriteRules(_Model):
    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)


class ReviewRules(_Model):
    required_reviewers: list[str] = Field(default_factory=list)
    approval_required_for_merge: bool = True
    min_approvals: int = 1


class DeployRules(_Model):
    allow_agent_deploy: bool = False
    environments: list[str] = Field(default_factory=list)
    restricted_environments: list[str] = Field(default_factory=list)


class KnowledgeRules(_Model):
    index_paths: list[str] = Field(default_factory=list)
    exclude_paths: list[str] = Field(default_factory=list)
    freshness_sla_hours: int | None = None


class PolicySkillProfiles(_Model):
    default: str | None = None
    allowed: list[str] = Field(default_factory=list)


class SubagentRules(_Model):
    allow_subagents: bool = False
    allowed_roles: list[str] = Field(default_factory=list)
    max_parallel: int = 0


class PolicySandboxBlock(_Model):
    """The ``sandbox:`` block of ``.forge/policy.yaml`` (F19).

    A repo may *strengthen* isolation above the workspace minimum but never
    weaken it (enforced by ``forge_agent.sandbox.selection.resolve_sandbox_kind``).
    ``isolation``/``network`` are stored as plain strings (``worktree``/
    ``container``/``gvisor``/``microvm``, ``none``/``egress``) to keep the
    contract dependency-free; they are validated when resolved into a
    ``SandboxSpec``.
    """

    isolation: str | None = None  # worktree | container | gvisor | microvm (request)
    image: str | None = None  # optional; must be on the workspace allowlist
    network: str | None = None  # none | egress
    egress_allowlist: list[str] = Field(default_factory=list)
    cpus: float | None = None
    memory: str | None = None  # e.g. "4g", "512m"
    pids_limit: int | None = None
    tmpfs_mb: int | None = None
    exec_timeout_seconds: int | None = None
    setup_commands: list[str] = Field(default_factory=list)
    # --- F34 kernel-boundary additions (additive) ---
    gvisor_platform: str | None = None  # systrap | kvm | ptrace (isolation: gvisor)
    vm_vcpus: int | None = None  # guest vCPUs (isolation: microvm)
    vm_memory: str | None = None  # guest RAM, e.g. "4g" (isolation: microvm)


#: Whitelisted condition fields available to a :class:`ConditionalRule` (F29).
#: ``now`` is the runtime-supplied UTC clock (operand for the ``*_time_window``
#: ops); ``weekday`` (0=Mon..6=Sun) and ``hour`` (0..23, UTC) are derived from it.
POLICY_CONDITION_FIELDS: frozenset[str] = frozenset(
    {
        "action",
        "path",
        "file_ext",
        "environment",
        "command",
        "role",
        "skill_profile",
        "branch",
        "base_branch",
        "task_kind",
        "actor_role",
        "execution_mode",
        "labels",
        "repo_id",
        "now",
        "weekday",
        "hour",
    }
)

#: Action names a :class:`ConditionalRule.applies_to` entry may reference (or
#: ``"*"``). Conforms to the tool/action vocabulary the F04 evaluator governs.
KNOWN_ACTIONS: frozenset[str] = frozenset(
    {
        # write / filesystem (forge_policy.WRITE_ACTIONS)
        "write_code",
        "write_file",
        "write",
        "edit",
        "edit_file",
        "create_file",
        "apply_patch",
        "modify_file",
        "move_file",
        "delete_file",
        "delete_files",
        # deploy / promotion
        "deploy",
        "promote_environment",
        # command / test execution
        "run_command",
        "run_tests",
        "run_test",
        "shell",
        # repo / VCS
        "read_repo",
        "read_file",
        "open_pr",
        "create_branch",
        "merge",
        "push",
        # knowledge / mcp
        "read_knowledge",
        "query_mcp",
        # multi-agent
        "spawn_subagent",
    }
)


def _condition_fields(group: ConditionGroup) -> list[str]:
    """Flatten every ``Condition.field`` referenced in a (nested) group."""
    fields = [c.field for c in group.conditions]
    for sub in group.groups:
        fields.extend(_condition_fields(sub))
    return fields


class ConditionalRule(_Model):
    """A declarative conditional refinement of the flat F04 policy (F29).

    A rule whose ``when`` predicate matches the runtime ``PolicyContext`` applies
    its ``effect`` under the evaluator's precedence ladder. ``override_base`` is
    only meaningful for ``effect=allow``: it permits *loosening* a NON-critical
    base denial (never a critical floor: path traversal, secret files, the merge
    gate). ``extra="forbid"`` so a typo cannot smuggle an unrecognised field.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=120)
    description: str | None = None
    applies_to: list[str] = Field(default_factory=lambda: ["*"])
    when: ConditionGroup = Field(default_factory=ConditionGroup)
    effect: RuleEffect
    severity: Severity = "warning"
    reason: str = Field(min_length=1)
    priority: int = 100
    override_base: bool = False
    enabled: bool = True


class Policy(_Model):
    """Parsed ``.forge/policy.yaml`` (spec: policy.yaml Schema).

    F29 adds the optional, schema-versioned ``rules`` block of conditional rules.
    A ``schema_version: 1`` policy (no ``rules``) evaluates byte-for-byte like F04.
    """

    repo_id: str
    schema_version: int = 1
    name: str | None = None
    purpose: str | None = None
    languages: list[str] = Field(default_factory=list)
    entrypoints: list[str] = Field(default_factory=list)
    commands: dict[str, str] = Field(default_factory=dict)
    write_rules: WriteRules = Field(default_factory=WriteRules)
    review_rules: ReviewRules = Field(default_factory=ReviewRules)
    deploy_rules: DeployRules = Field(default_factory=DeployRules)
    knowledge_rules: KnowledgeRules = Field(default_factory=KnowledgeRules)
    skill_profiles: PolicySkillProfiles = Field(default_factory=PolicySkillProfiles)
    subagent_rules: SubagentRules = Field(default_factory=SubagentRules)
    sandbox: PolicySandboxBlock | None = None
    allowed_actions: list[str] = Field(default_factory=list)
    restricted_actions: list[str] = Field(default_factory=list)
    rules: list[ConditionalRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_rules(self) -> Policy:
        """Fail-closed validation of the conditional ``rules`` block (F29).

        * non-empty ``rules`` require ``schema_version >= 2``;
        * rule ``id``s must be unique;
        * every ``Condition.field`` must be in :data:`POLICY_CONDITION_FIELDS`;
        * every ``applies_to`` entry must be a known action or ``"*"``.
        """
        if not self.rules:
            return self
        if self.schema_version < 2:
            raise ValueError("conditional 'rules' require schema_version >= 2")

        seen: set[str] = set()
        for rule in self.rules:
            if rule.id in seen:
                raise ValueError(f"duplicate conditional rule id {rule.id!r}")
            seen.add(rule.id)
            for field in _condition_fields(rule.when):
                if field not in POLICY_CONDITION_FIELDS:
                    raise ValueError(
                        f"rule {rule.id!r}: condition field {field!r} is not a known "
                        "policy condition field"
                    )
            for action in rule.applies_to:
                if action != "*" and action not in KNOWN_ACTIONS:
                    raise ValueError(
                        f"rule {rule.id!r}: applies_to {action!r} is not a known action or '*'"
                    )
        return self


# --------------------------------------------------------------------------- #
# Spec engine                                                                  #
# --------------------------------------------------------------------------- #


class Requirement(_Model):
    id: str
    text: str


class OpenQuestion(_Model):
    id: str
    text: str
    resolution: str | None = None


class ADR(_Model):
    """An architecture decision record (spec: ``decisions[]``)."""

    id: str
    title: str
    status: str = "proposed"
    context: str | None = None
    decision: str | None = None
    consequences: str | None = None


class Constitution(_Model):
    """Engineering principles / architecture guardrails for a project."""

    id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    principles: list[str] = Field(default_factory=list)
    architecture_guardrails: list[str] = Field(default_factory=list)
    content: str | None = None


class SpecManifest(_Model):
    """Machine-readable spec metadata (spec: Spec Manifest Schema)."""

    id: str
    name: str
    status: SpecStatus = SpecStatus.DRAFT
    constitution_refs: list[str] = Field(default_factory=list)
    repos: list[str] = Field(default_factory=list)
    requirements: list[Requirement] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    decisions: list[ADR] = Field(default_factory=list)
    plan_ref: str | None = None
    tasks_ref: str | None = None
    validation_ref: str | None = None
    execution_mode: ExecutionMode = ExecutionMode.SINGLE_AGENT
    skill_profile: str | None = None


class RequirementTrace(_Model):
    """Requirement -> acceptance -> task -> test traceability row."""

    requirement_id: str
    text: str | None = None
    acceptance_criteria_ids: list[str] = Field(default_factory=list)
    task_refs: list[str] = Field(default_factory=list)
    test_refs: list[str] = Field(default_factory=list)
    satisfied: bool = False


class CheckResult(_Model):
    """A single verification check outcome (lint / type / tests / coverage)."""

    name: str
    passed: bool
    details: str | None = None


class ValidationReport(_Model):
    """Output of ``SpecEngine.validate`` (spec: validation / traceability)."""

    task_id: str
    spec_id: str | None = None
    passed: bool = False
    traceability: list[RequirementTrace] = Field(default_factory=list)
    checks: list[CheckResult] = Field(default_factory=list)
    coverage: float | None = None
    notes: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Board entities                                                               #
# --------------------------------------------------------------------------- #


class EpicDTO(_Model):
    id: uuid.UUID | None = None
    key: str | None = None
    project_id: uuid.UUID | None = None
    title: str
    description: str | None = None
    status: str = "open"
    spec_id: uuid.UUID | None = None
    labels: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class TaskDTO(_Model):
    """A board task (spec: Task Schema)."""

    id: uuid.UUID | None = None
    key: str | None = None
    project_id: uuid.UUID | None = None
    epic_id: uuid.UUID | None = None
    spec_id: uuid.UUID | None = None
    kind: TaskKind = TaskKind.FEATURE
    title: str
    description: str | None = None
    status: TaskStatus = TaskStatus.BACKLOG
    priority: Priority = Priority.MEDIUM
    estimate: int | None = None
    execution_mode: ExecutionMode = ExecutionMode.SINGLE_AGENT
    repo_targets: list[RepoTarget] = Field(default_factory=list)
    instructions_profile: str | None = None
    skill_profile: str | None = None
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    allowed_actions: list[str] = Field(default_factory=list)
    restricted_actions: list[str] = Field(default_factory=list)
    requires_approval: ApprovalPolicy = Field(default_factory=ApprovalPolicy)
    knowledge_scope: KnowledgeScope | None = None
    subagent_policy: SubAgentPolicy = Field(default_factory=SubAgentPolicy)
    handoff_rules: HandoffRules | None = None
    labels: list[str] = Field(default_factory=list)
    assignee_id: uuid.UUID | None = None
    sprint_id: uuid.UUID | None = None
    milestone_id: uuid.UUID | None = None
    depends_on: list[uuid.UUID] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SprintDTO(_Model):
    id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    name: str
    goal: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    task_ids: list[uuid.UUID] = Field(default_factory=list)


class MilestoneDTO(_Model):
    id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    name: str
    description: str | None = None
    due_at: datetime | None = None


class IncidentDTO(_Model):
    id: uuid.UUID | None = None
    key: str | None = None
    project_id: uuid.UUID | None = None
    title: str
    description: str | None = None
    severity: IncidentSeverity = IncidentSeverity.MEDIUM
    state: IncidentState = IncidentState.ALERT_RECEIVED
    created_at: datetime | None = None
    updated_at: datetime | None = None


class BoardFilter(_Model):
    """A saved/board filter (spec: Board Features — saved filters)."""

    project_id: uuid.UUID | None = None
    statuses: list[str] = Field(default_factory=list)
    kinds: list[TaskKind] = Field(default_factory=list)
    priorities: list[Priority] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    assignee_id: uuid.UUID | None = None
    sprint_id: uuid.UUID | None = None
    epic_id: uuid.UUID | None = None
    text: str | None = None
    limit: int | None = None
    offset: int = 0


class BulkUpdate(_Model):
    """One entry in a bulk board mutation (spec: Bulk actions)."""

    task_id: uuid.UUID
    status: TaskStatus | None = None
    priority: Priority | None = None
    assignee_id: uuid.UUID | None = None
    sprint_id: uuid.UUID | None = None
    labels: list[str] | None = None


# --------------------------------------------------------------------------- #
# Approvals                                                                    #
# --------------------------------------------------------------------------- #


class ApprovalRequest(_Model):
    """A human approval gate (spec: Human Approval System / Approval UI Must Show)."""

    id: uuid.UUID | None = None
    gate: ApprovalGate
    status: ApprovalStatus = ApprovalStatus.PENDING
    task_id: uuid.UUID | None = None
    workflow_run_id: uuid.UUID | None = None
    agent_run_id: uuid.UUID | None = None
    title: str | None = None
    summary: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    verification: list[CheckResult] = Field(default_factory=list)
    spec_traceability: list[RequirementTrace] = Field(default_factory=list)
    knowledge_provenance: list[str] = Field(default_factory=list)
    confidence: float | None = None
    risks: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)
    requested_by: str | None = None
    created_at: datetime | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_reason: str | None = None


# --------------------------------------------------------------------------- #
# Workflow engine                                                              #
# --------------------------------------------------------------------------- #


class WorkflowTransition(_Model):
    """A single FSM transition (spec: Workflow DSL ``transitions[]``).

    Uses ``from``/``to`` aliases so the DSL parses verbatim.
    """

    from_state: str = Field(alias="from")
    to_state: str = Field(alias="to")
    action: str | None = None
    when: str | list[str] | None = None
    condition: str | None = None
    preconditions: list[str] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)
    record: str | None = None
    skill: str | None = None


class RetryPolicy(_Model):
    max_retries: int = DEFAULT_MAX_RETRIES
    backoff: str = "exponential"
    initial_delay_seconds: int = 30


class EscalationPolicy(_Model):
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
    on_low_confidence: str = "pause_and_notify"
    on_policy_conflict: str = "escalate_to_admin"


class WorkflowDefinition(_Model):
    """A parsed workflow DSL document (spec: Workflow DSL)."""

    name: str
    version: str = "1"
    modes: dict[str, Any] = Field(default_factory=dict)
    transitions: list[WorkflowTransition] = Field(default_factory=list)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    escalation_policy: EscalationPolicy = Field(default_factory=EscalationPolicy)


class WorkflowRun(_Model):
    """A durable FSM run (spec: WorkflowRun). Return type of ``WorkflowEngine.start``."""

    id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    workflow_name: str = "default_feature"
    current_state: str = "created"
    execution_mode: ExecutionMode = ExecutionMode.SINGLE_AGENT
    status: RunStatus = RunStatus.PENDING
    context: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    completed_at: datetime | None = None


# --------------------------------------------------------------------------- #
# MCP                                                                          #
# --------------------------------------------------------------------------- #


class MCPAuth(_Model):
    type: MCPAuthType = MCPAuthType.NONE
    token_ref: str | None = None
    # RFC 8707 resource parameter — binds the token to a specific server.
    resource: str | None = None


class MCPCapabilities(_Model):
    resources: bool = False
    tools: bool = False
    prompts: bool = False


class MCPConnection(_Model):
    """A registered MCP server (spec: MCP Connection Schema)."""

    id: str
    name: str
    transport: MCPTransport = MCPTransport.HTTP
    endpoint: str | None = None
    auth: MCPAuth = Field(default_factory=MCPAuth)
    capabilities: MCPCapabilities = Field(default_factory=MCPCapabilities)
    sync_mode: SyncMode = SyncMode.INCREMENTAL
    index_strategy: MCPIndexStrategy = MCPIndexStrategy.SYNC_AND_INDEX
    freshness_sla_minutes: int | None = None
    # Spec MCP security rule 1: MUST default to false.
    allow_write: bool = False
    allowed_namespaces: list[str] = Field(default_factory=list)


class MCPResource(_Model):
    uri: str
    name: str | None = None
    namespace: str | None = None
    mime_type: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MCPResourceContent(_Model):
    uri: str
    content: str
    mime_type: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MCPToolResult(_Model):
    tool: str
    status: str = "ok"
    content: Any | None = None
    error: str | None = None
    latency_ms: int | None = None
    payload_hash: str | None = None


class MCPAuditEntry(_Model):
    """An immutable MCP audit record (spec: MCP security rule 4 — full audit log)."""

    connection_id: str
    tool: str
    payload_hash: str
    status: str
    latency_ms: int | None = None
    timestamp: datetime | None = None
    redacted: bool = True


# --------------------------------------------------------------------------- #
# Integrations (GitHub / Slack / PM adapter)                                   #
# --------------------------------------------------------------------------- #


class RepositoryConnection(_Model):
    """A connected source repository (GitHub App installation)."""

    id: uuid.UUID | None = None
    provider: RepoProvider = RepoProvider.GITHUB
    full_name: str
    installation_id: str | None = None
    default_branch: str = "main"
    clone_url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RepoSyncResult(_Model):
    repo: str
    head_sha: str | None = None
    files_changed: int = 0
    indexed: int = 0
    deleted: int = 0


class PullRequestRequest(_Model):
    repo: str
    title: str
    body: str | None = None
    head: str
    base: str = "main"
    draft: bool = False
    reviewers: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)


class PullRequest(_Model):
    repo: str
    number: int | None = None
    url: str | None = None
    state: PRState = PRState.OPEN
    title: str | None = None
    head: str | None = None
    base: str = "main"
    head_sha: str | None = None


class CIStatus(_Model):
    """Parsed CI / check-run webhook payload."""

    repo: str
    sha: str
    state: CIState = CIState.PENDING
    context: str | None = None
    description: str | None = None
    target_url: str | None = None
    checks: list[CheckResult] = Field(default_factory=list)


class SlackMessage(_Model):
    channel: str
    text: str
    blocks: list[dict[str, Any]] | None = None
    thread_ts: str | None = None


class SlackDeliveryResult(_Model):
    ok: bool
    channel: str | None = None
    ts: str | None = None
    error: str | None = None


class ExternalTask(_Model):
    """A task in an external PM system (spec: External PM Adapter Contract)."""

    external_id: str
    system: str
    title: str | None = None
    status: str | None = None
    priority: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)


class WebhookEvent(_Model):
    """A normalised inbound webhook (GitHub/Slack/PM)."""

    source: str
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    signature: str | None = None
    received_at: datetime | None = None


class HealthResult(_Model):
    healthy: bool
    status: str = "ok"
    latency_ms: int | None = None
    message: str | None = None
    checked_at: datetime | None = None


# ``ForgeTask`` in the spec's PMAdapter contract is the Forge-side task DTO.
ForgeTask = TaskDTO


# --------------------------------------------------------------------------- #
# Model client (BYOK, provider-agnostic)                                       #
# --------------------------------------------------------------------------- #


class ModelMessage(_Model):
    role: str
    content: str


class ModelToolCall(_Model):
    """A tool call emitted by a model response (distinct from the agent ``ToolCall``)."""

    id: str | None = None
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class TokenUsage(_Model):
    input_tokens: int = 0
    output_tokens: int = 0


class ModelRequest(_Model):
    model: str
    messages: list[ModelMessage] = Field(default_factory=list)
    system: str | None = None
    tools: list[dict[str, Any]] = Field(default_factory=list)
    max_tokens: int | None = None
    temperature: float | None = None
    stop: list[str] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelResponse(_Model):
    content: str = ""
    model: str | None = None
    stop_reason: str | None = None
    tool_calls: list[ModelToolCall] = Field(default_factory=list)
    usage: TokenUsage | None = None


class ModelStreamEvent(_Model):
    type: str
    text: str | None = None
    delta: str | None = None


__all__ = [
    "ADR",
    "AcceptanceCriterion",
    "AgentObjective",
    "AgentRunResult",
    "ApprovalPolicy",
    "ApprovalRequest",
    "BoardFilter",
    "BulkUpdate",
    "CIStatus",
    "CheckResult",
    "Chunk",
    "Constitution",
    "CrossPRLink",
    "Decision",
    "DeployRules",
    "EpicDTO",
    "EscalationPolicy",
    "ExternalTask",
    "ForgeTask",
    "HandoffRules",
    "HealthResult",
    "IncidentDTO",
    "IndexResult",
    "KnowledgeRules",
    "KnowledgeScope",
    "MCPAuditEntry",
    "MCPAuth",
    "MCPCapabilities",
    "MCPConnection",
    "MCPResource",
    "MCPResourceContent",
    "MCPToolResult",
    "MergeGroupOutcome",
    "MergePlan",
    "MilestoneDTO",
    "ModelMessage",
    "ModelRequest",
    "ModelResponse",
    "ModelStreamEvent",
    "ModelToolCall",
    "MultiRepoMergeGateResult",
    "OpenQuestion",
    "PRGroup",
    "Policy",
    "PolicySkillProfiles",
    "PullRequest",
    "PullRequestRequest",
    "Ranked",
    "RepoChangeSet",
    "RepoMergeStatus",
    "RepoSyncResult",
    "RepoTarget",
    "RepositoryConnection",
    "Requirement",
    "RequirementTrace",
    "RerankResult",
    "RetrievedChunk",
    "ReviewRules",
    "SkillProfile",
    "SlackDeliveryResult",
    "SlackMessage",
    "SpecManifest",
    "SprintDTO",
    "Step",
    "SubAgentPolicy",
    "SubagentRules",
    "TaskDTO",
    "TokenUsage",
    "ToolCall",
    "ValidationReport",
    "WebhookEvent",
    "WorkflowDefinition",
    "WorkflowRun",
    "WorkflowTransition",
    "WriteRules",
]
