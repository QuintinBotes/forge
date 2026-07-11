"""Injected dependencies for the Supervisor (F27 §4 ``CoordinatorDeps``).

The supervisor graph itself routes by explicit policy, never LLM judgement
(AC 2) — the nodes never call a model. The LLM work happens inside each
subagent, built by ``agent_factory``. When an Adaptive Orchestration
:class:`~forge_agent.ExecutionPlan` pins a per-role model, an optional
``model_client_factory`` turns that concrete model string into a
``ModelClient`` (via the HARD-02 ``ModelClientConfig`` -> ``build_model_client``
seam) so different roles can run against different models; the deterministic
graph only *selects* the model, it never invokes it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from forge_agent import ExecutionPlan
from forge_contracts import AgentRuntime, ModelClient, Step
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

    #: Build a subagent runtime for one dispatch. Receives the per-role
    #: :class:`~forge_contracts.ModelClient` (from ``model_client_factory`` when a
    #: plan pins a model) or ``None`` — in which case the factory falls back to
    #: its own default client, preserving single-agent/back-compat callers.
    agent_factory: Callable[[ModelClient | None], AgentRuntime]
    #: Turn a concrete per-role model string (``RoleExecution.model``, resolved by
    #: :class:`~forge_agent.ModelRouter`) into a ``ModelClient`` via the HARD-02
    #: ``ModelClientConfig`` -> ``build_model_client`` seam. ``None`` keeps every
    #: subagent on ``agent_factory``'s default model (no per-role routing).
    model_client_factory: Callable[[str], ModelClient] | None = None
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
