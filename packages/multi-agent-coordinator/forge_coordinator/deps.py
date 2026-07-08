"""Injected dependencies for the Supervisor (F27 §4 ``CoordinatorDeps``).

The dependency set intentionally carries **no model client / model_factory** —
the supervisor graph routes by explicit policy, never LLM judgement (AC 2). The
LLM work happens inside each subagent, built by ``agent_factory``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from forge_agent import ExecutionPlan
from forge_contracts import AgentRuntime, Step
from forge_coordinator.merger import BranchMerger
from forge_coordinator.persistence import InMemorySubAgentRunSink, SubAgentRunSink
from forge_coordinator.selector import DefaultPatternSelector, PatternSelector
from forge_coordinator.settings import CoordinatorSettings
from forge_coordinator.workspace import SubAgentWorkspaceManager

__all__ = ["CoordinatorDeps"]


def _default_workspace_factory(repo: str) -> SubAgentWorkspaceManager:
    return SubAgentWorkspaceManager(repo)


@dataclass
class CoordinatorDeps:
    """Everything the Supervisor needs, all injectable for tests."""

    agent_factory: Callable[[], AgentRuntime]
    pattern_selector: PatternSelector = field(default_factory=DefaultPatternSelector)
    merger: BranchMerger = field(default_factory=BranchMerger)
    sub_agent_sink: SubAgentRunSink = field(default_factory=InMemorySubAgentRunSink)
    settings: CoordinatorSettings = field(default_factory=CoordinatorSettings)
    workspace_factory: Callable[[str], SubAgentWorkspaceManager] = _default_workspace_factory
    step_sink: Callable[[Step], None] | None = None
    audit_sink: Callable[[dict], None] | None = None
    #: Adaptive Orchestration plan (ao-policy). When present it gates swarm
    #: fan-out (``strategy=single`` forces a single agent), scales the review
    #: loop budget with complexity, and pins each subagent's per-role model.
    execution_plan: ExecutionPlan | None = None
