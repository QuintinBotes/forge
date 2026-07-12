"""Supervised multi-agent coordination for Forge (F27).

A **deterministic** Supervisor (LangGraph ``StateGraph`` with zero LLM calls)
selects a coordination pattern from explicit policy, spawns context-isolated,
role-scoped specialist subagents (each a reused F06 ``AgentRuntime`` in its own
git worktree), merges code-producing outputs onto an integration branch (conflicts
-> human interrupt), validates against the approved spec, and returns an
``AgentRunResult`` byte-compatible with the F08 verify->PR->approval flow.
"""

from __future__ import annotations

from forge_coordinator.aggregate import (
    AcceptanceCheck,
    aggregate_confidence,
    validate_acceptance,
)
from forge_coordinator.deps import CoordinatorDeps
from forge_coordinator.graph import build_resume_graph, build_supervisor_graph
from forge_coordinator.merger import BranchMerger
from forge_coordinator.objectives import build_subagent_objective
from forge_coordinator.persistence import (
    InMemorySubAgentRunSink,
    SqlAlchemySubAgentRunSink,
    SubAgentRunCreate,
    SubAgentRunSink,
)
from forge_coordinator.policy_gate import GateDecision, evaluate_gate
from forge_coordinator.red_team import (
    FailingTestRef,
    HomogeneousAdversaryError,
    RedTeamError,
    RedTeamResult,
    SpecViolation,
    run_red_team,
)
from forge_coordinator.selector import (
    DefaultPatternSelector,
    PatternSelector,
    resolve_allowed_actions,
    resolve_max_parallel,
)
from forge_coordinator.settings import CoordinatorSettings
from forge_coordinator.state import SupervisionState
from forge_coordinator.supervisor import HumanResumeInput, Supervisor
from forge_coordinator.workspace import SubAgentWorkspaceManager, WorktreeHandle

__version__ = "0.1.0"

__all__ = [
    "AcceptanceCheck",
    "BranchMerger",
    "CoordinatorDeps",
    "CoordinatorSettings",
    "DefaultPatternSelector",
    "FailingTestRef",
    "GateDecision",
    "HomogeneousAdversaryError",
    "HumanResumeInput",
    "InMemorySubAgentRunSink",
    "PatternSelector",
    "RedTeamError",
    "RedTeamResult",
    "SpecViolation",
    "SqlAlchemySubAgentRunSink",
    "SubAgentRunCreate",
    "SubAgentRunSink",
    "SubAgentWorkspaceManager",
    "SupervisionState",
    "Supervisor",
    "WorktreeHandle",
    "aggregate_confidence",
    "build_resume_graph",
    "build_subagent_objective",
    "build_supervisor_graph",
    "evaluate_gate",
    "resolve_allowed_actions",
    "resolve_max_parallel",
    "run_red_team",
    "validate_acceptance",
]
