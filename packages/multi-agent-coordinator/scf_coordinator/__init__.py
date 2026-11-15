"""Supervised multi-agent coordination for SCF (F27).

A **deterministic** Supervisor (LangGraph ``StateGraph`` with zero LLM calls)
selects a coordination pattern from explicit policy, spawns context-isolated,
role-scoped specialist subagents (each a reused F06 ``AgentRuntime`` in its own
git worktree), merges code-producing outputs onto an integration branch (conflicts
-> human interrupt), validates against the approved spec, and returns an
``AgentRunResult`` byte-compatible with the F08 verify->PR->approval flow.
"""

from __future__ import annotations

from scf_coordinator.aggregate import (
    AcceptanceCheck,
    aggregate_confidence,
    validate_acceptance,
)
from scf_coordinator.deps import CoordinatorDeps
from scf_coordinator.graph import build_resume_graph, build_supervisor_graph
from scf_coordinator.merger import BranchMerger
from scf_coordinator.objectives import build_subagent_objective
from scf_coordinator.persistence import (
    InMemorySubAgentRunSink,
    SqlAlchemySubAgentRunSink,
    SubAgentRunCreate,
    SubAgentRunSink,
)
from scf_coordinator.policy_gate import GateDecision, evaluate_gate
from scf_coordinator.red_team import (
    FailingTestRef,
    HomogeneousAdversaryError,
    RedTeamError,
    RedTeamResult,
    SpecViolation,
    run_red_team,
)
from scf_coordinator.selector import (
    DefaultPatternSelector,
    PatternSelector,
    resolve_allowed_actions,
    resolve_max_parallel,
)
from scf_coordinator.settings import CoordinatorSettings
from scf_coordinator.state import SupervisionState
from scf_coordinator.supervisor import HumanResumeInput, Supervisor
from scf_coordinator.workspace import SubAgentWorkspaceManager, WorktreeHandle

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
