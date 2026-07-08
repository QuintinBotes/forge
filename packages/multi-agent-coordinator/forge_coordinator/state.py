"""The typed supervision state carried through the supervisor graph (F27 §3.3).

Every router is a pure predicate over this state; every node mutates it
deterministically. The state holds in-memory orchestration handles (worktrees,
the workspace manager) — it is the single authoritative supervision object for one
``Supervisor.run``/``resume``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from forge_agent import ExecutionPlan
from forge_contracts import (
    AgentObjective,
    AgentRunResult,
    MergeResult,
    Step,
    SubAgentAssignment,
    SubAgentPolicy,
    SubAgentResult,
    SubagentRules,
    SupervisionPlan,
)
from forge_coordinator.aggregate import AcceptanceCheck
from forge_coordinator.workspace import SubAgentWorkspaceManager, WorktreeHandle
from forge_skill.directives import SkillDirectives

#: Per-assignment lifecycle status (superset of the persisted RunStatus).
AssignmentStatus = str  # pending|running|succeeded|failed|blocked|skipped|awaiting_input


@dataclass
class SupervisionState:
    """The mutable state of one supervised run."""

    objective: AgentObjective
    parent_agent_run_id: uuid.UUID
    workspace_id: uuid.UUID
    subagent_rules: SubagentRules
    task_subagent_policy: SubAgentPolicy
    directives: SkillDirectives
    threshold: float

    repo: str | None = None
    base_branch: str = "main"
    integration_branch: str = "forge/integration"
    base_sha: str | None = None

    plan: SupervisionPlan | None = None
    assignments: dict[str, SubAgentAssignment] = field(default_factory=dict)
    statuses: dict[str, AssignmentStatus] = field(default_factory=dict)
    results: dict[str, SubAgentResult] = field(default_factory=dict)
    row_ids: dict[str, uuid.UUID] = field(default_factory=dict)
    handles: dict[str, WorktreeHandle] = field(default_factory=dict)

    superseded: set[str] = field(default_factory=set)
    processed_rejects: set[str] = field(default_factory=set)
    review_loops: int = 0
    review_loop_budget: int = 1
    max_parallel: int = 1

    merge_result: MergeResult | None = None
    acceptance_checks: list[AcceptanceCheck] = field(default_factory=list)
    aggregate_confidence: float | None = None

    policy_conflict: str | None = None
    needs_human: bool = False
    needs_human_reason: str | None = None
    error: str | None = None

    steps: list[Step] = field(default_factory=list)
    ws_manager: SubAgentWorkspaceManager | None = None
    result: AgentRunResult | None = None

    #: Adaptive Orchestration plan (ao-policy) resolved for this run, if any.
    execution_plan: ExecutionPlan | None = None

    # ------------------------------------------------------------------ #
    def ready_assignments(self) -> list[SubAgentAssignment]:
        """Pending assignments whose deps are all satisfied (succeeded/skipped)."""
        ready: list[SubAgentAssignment] = []
        for aid, status in self.statuses.items():
            if status != "pending":
                continue
            assignment = self.assignments[aid]
            if all(
                self.statuses.get(dep) in {"succeeded", "skipped"} for dep in assignment.depends_on
            ):
                ready.append(assignment)
        ready.sort(key=lambda a: (a.ordinal, a.id))
        return ready

    def has_pending(self) -> bool:
        return any(s == "pending" for s in self.statuses.values())
