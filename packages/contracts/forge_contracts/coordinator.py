"""Frozen DTOs for the supervised multi-agent coordinator (F27).

These types are the contract between the deterministic Supervisor
(``forge_coordinator``) and its consumers (the API read endpoints, F10 trace
nesting, and F08's verify->PR flow which consumes the returned
``AgentRunResult``). They are intentionally provider-agnostic and carry **no**
model client — the Supervisor routes by explicit policy, never LLM judgement.

Foundation conformance notes:

* The ``AgentRuntime`` protocol in this repo is the synchronous
  ``run(objective) -> AgentRunResult`` (see ``forge_contracts.protocols``); the
  Supervisor implements it verbatim. ``branch_name`` / ``diff_stat`` /
  ``head_commit_sha`` are carried on ``AgentRunResult.repo_change_sets`` (the
  foundation shape), not as flat fields.
* The task-level sub-agent policy reuses the frozen
  :class:`forge_contracts.SubAgentPolicy` (``allowed`` / ``allowed_roles`` /
  ``max_parallel``) rather than introducing a parallel ``SubagentPolicy`` type.
* The ``PatternSelector`` Protocol and ``DefaultPatternSelector`` live in
  ``forge_coordinator`` (they depend on ``forge_skill`` directives, which would
  invert this package's dependency direction).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from forge_contracts.dtos import AcceptanceCriterion, RetrievedChunk, TokenUsage
from forge_contracts.enums import SubAgentRole

__all__ = [
    "CODE_PRODUCING_ROLES",
    "READ_ONLY_ROLES",
    "ROLE_TOOLS",
    "CoordinationPattern",
    "MergeConflict",
    "MergeResult",
    "SubAgentArtifact",
    "SubAgentAssignment",
    "SubAgentResult",
    "SupervisionPlan",
    "SupervisionView",
]


class _CoordModel(BaseModel):
    """Shared config for coordinator DTOs (mirrors ``forge_contracts`` style)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class CoordinationPattern(StrEnum):
    """The five built-in supervisor coordination patterns (spec: Pattern Guide)."""

    ORCHESTRATOR_WORKER = "orchestrator_worker"  # degenerate: one implementer
    SEQUENTIAL_PIPELINE = "sequential_pipeline"  # research->plan->impl->test->review
    FAN_OUT_FAN_IN = "fan_out_fan_in"  # parallel independent implementers
    MAKER_CHECKER = "maker_checker"  # implementer + reviewer loop
    DYNAMIC_HANDOFF = "dynamic_handoff"  # supervisor re-routes by explicit rules


#: Spec "Subagent Role Definitions" -> "Scoped Tools Only". Each role's tool set
#: is intersected with ``task.allowed_actions`` and the skill allowlist at spawn;
#: it is never widened (no scope self-expansion).
ROLE_TOOLS: dict[SubAgentRole, frozenset[str]] = {
    SubAgentRole.PLANNER: frozenset({"read_repo", "read_spec", "read_knowledge", "write_spec"}),
    SubAgentRole.RESEARCHER: frozenset({"read_repo", "search_knowledge", "query_mcp"}),
    SubAgentRole.IMPLEMENTER: frozenset({"read_repo", "write_code", "run_tests", "open_pr"}),
    SubAgentRole.TESTER: frozenset({"read_repo", "write_tests", "run_tests"}),
    SubAgentRole.REVIEWER: frozenset({"read_repo", "read_spec", "write_review_comment"}),
    SubAgentRole.SECURITY: frozenset({"read_repo", "run_sast", "audit_dependencies"}),
    # Red-Team Gate: the adversary may READ the diff, AUTHOR + RUN a failing test,
    # and run SAST — but never edit product code (no ``write_code``/``open_pr``).
    SubAgentRole.ADVERSARY: frozenset({"read_repo", "run_tests", "write_test", "run_sast"}),
}

#: Roles whose output is a code change that must be merged onto the integration
#: branch (everything else is a read-only / structured-artifact role).
CODE_PRODUCING_ROLES: frozenset[SubAgentRole] = frozenset(
    {SubAgentRole.IMPLEMENTER, SubAgentRole.TESTER}
)
READ_ONLY_ROLES: frozenset[SubAgentRole] = frozenset(
    {
        SubAgentRole.PLANNER,
        SubAgentRole.RESEARCHER,
        SubAgentRole.REVIEWER,
        SubAgentRole.SECURITY,
        # The adversary WRITES a test but that test is executed in a sandbox and
        # folded into a structured ``red_team_report`` — it is never merged onto
        # the integration branch, so it is a read-only / structured-artifact role.
        SubAgentRole.ADVERSARY,
    }
)


class SubAgentAssignment(_CoordModel):
    """One role-scoped unit of work in a :class:`SupervisionPlan`."""

    id: str  # plan-local id, e.g. "sa-implementer-1"
    role: SubAgentRole
    objective: str  # role-scoped objective text
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    allowed_actions: list[str] = Field(default_factory=list)  # RESOLVED intersection
    context_refs: list[RetrievedChunk] = Field(default_factory=list)  # ISOLATED scoped context
    depends_on: list[str] = Field(default_factory=list)  # assignment ids (DAG edges)
    ordinal: int = 0
    optional: bool = False  # failure does not fail the parent run


class SupervisionPlan(_CoordModel):
    """The deterministic plan a :class:`PatternSelector` produces."""

    pattern: CoordinationPattern
    assignments: list[SubAgentAssignment] = Field(default_factory=list)
    max_parallel: int = 1  # resolved & capped
    review_loop_budget: int = 1
    merge_strategy: Literal["sequential_integration", "fan_in_merge", "read_only"] = (
        "sequential_integration"
    )


class SubAgentArtifact(_CoordModel):
    """A STRUCTURED role output, never free-form chat (Multi-Agent Rule)."""

    kind: Literal[
        "code_change",
        "review",
        "test_suite",
        "spec_draft",
        "research_brief",
        "security_report",
        "red_team_report",
    ]
    summary: str = ""
    review_verdict: Literal["approved", "changes_requested"] | None = None
    findings: list[str] = Field(default_factory=list)
    branch_name: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    report_ref: str | None = None  # object-store ref (e.g. SARIF for security)


class SubAgentResult(_CoordModel):
    """The folded outcome of one spawned subagent."""

    assignment_id: str
    role: SubAgentRole
    agent_run_id: UUID | None = None  # child run id
    status: Literal["succeeded", "failed", "blocked", "skipped", "awaiting_input"] = "succeeded"
    confidence: float = 0.0
    artifact: SubAgentArtifact
    token_usage: TokenUsage = Field(default_factory=TokenUsage)


class MergeConflict(_CoordModel):
    """A single unresolved conflict surfaced for human review (never auto-resolved)."""

    assignment_id: str
    path: str
    detail: str = ""


class MergeResult(_CoordModel):
    """The outcome of merging code-producing subagent branches onto integration."""

    integration_branch: str
    head_sha: str | None = None
    merged_assignments: list[str] = Field(default_factory=list)
    conflicts: list[MergeConflict] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    diff_stat: dict[str, int] = Field(default_factory=dict)

    @property
    def clean(self) -> bool:
        """True when the merge produced no conflicts."""
        return not self.conflicts


class SupervisionView(_CoordModel):
    """Read DTO for ``GET /agent-runs/{id}/supervision``."""

    parent_agent_run_id: UUID
    pattern: CoordinationPattern
    plan: SupervisionPlan
    subagents: list[SubAgentResult] = Field(default_factory=list)
    merge: MergeResult | None = None
    aggregate_confidence: float | None = None
    policy_conflict: str | None = None
