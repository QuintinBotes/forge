"""LangGraph-style single-agent execution loop, tool registry, and worktree sandbox.

Public surface:

* :class:`AgentRunner` — implements the frozen ``AgentRuntime`` protocol
  (``run(objective) -> AgentRunResult``) via a plan -> act -> observe graph.
* :class:`ToolRegistry` / :class:`Tool` / :class:`ToolResult` — policy-checked
  tool dispatch.
* :class:`PolicyGate` / :class:`ActionPolicyGate` / :class:`PolicyEvaluatorGate`.
* :class:`WorktreeSandbox` + :func:`load_agents_md` — git-worktree isolation.
* :class:`StateGraph` / :class:`CompiledGraph` — a ``langgraph``-backed graph engine.
"""

from __future__ import annotations

from forge_agent.context import build_system_prompt, skill_profile_directives
from forge_agent.graph import END, CompiledGraph, GraphError, StateGraph
from forge_agent.multi_repo import MultiRepoWorkspace, WorktreeHandle
from forge_agent.policy_gate import ActionPolicyGate, PolicyEvaluatorGate, PolicyGate
from forge_agent.policy_guard import MultiRepoPolicyGuard
from forge_agent.providers import (
    DEFAULT_TIER_MODELS,
    MODEL_PRICING,
    AnthropicModelClient,
    ModelClientConfig,
    ModelClientError,
    ModelClientUnavailable,
    ModelRouter,
    OpenAIModelClient,
    ProviderName,
    RouteDecision,
    UsageAccumulator,
    build_model_client,
    classify_tier,
    cost_usd,
    route,
)
from forge_agent.runtime import AgentRunner
from forge_agent.sandbox import SandboxError, WorktreeSandbox, load_agents_md
from forge_agent.state import AgentState
from forge_agent.tools import (
    FINISH_TOOL,
    Tool,
    ToolHandler,
    ToolRegistry,
    ToolResult,
    default_tool_registry,
)

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_TIER_MODELS",
    "END",
    "FINISH_TOOL",
    "MODEL_PRICING",
    "ActionPolicyGate",
    "AgentRunner",
    "AgentState",
    "AnthropicModelClient",
    "CompiledGraph",
    "GraphError",
    "ModelClientConfig",
    "ModelClientError",
    "ModelClientUnavailable",
    "ModelRouter",
    "MultiRepoPolicyGuard",
    "MultiRepoWorkspace",
    "OpenAIModelClient",
    "PolicyEvaluatorGate",
    "PolicyGate",
    "ProviderName",
    "RouteDecision",
    "SandboxError",
    "StateGraph",
    "Tool",
    "ToolHandler",
    "ToolRegistry",
    "ToolResult",
    "UsageAccumulator",
    "WorktreeHandle",
    "WorktreeSandbox",
    "build_model_client",
    "build_system_prompt",
    "classify_tier",
    "cost_usd",
    "default_tool_registry",
    "load_agents_md",
    "route",
    "skill_profile_directives",
]
