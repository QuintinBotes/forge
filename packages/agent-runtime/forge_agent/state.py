"""Mutable state carried through the agent graph."""

from __future__ import annotations

from dataclasses import dataclass, field

from forge_agent.providers.usage import UsageAccumulator
from forge_contracts import AgentObjective, ModelMessage, ModelToolCall, Step

__all__ = ["AgentState"]


@dataclass
class AgentState:
    """The single value threaded through plan -> act -> observe nodes."""

    objective: AgentObjective
    system: str = ""
    messages: list[ModelMessage] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)

    #: Tool calls the model emitted that are still pending dispatch.
    pending: list[ModelToolCall] = field(default_factory=list)
    last_content: str = ""
    iteration: int = 0
    max_iterations: int = 12
    finished: bool = False

    # Accumulated outcome.
    output: str | None = None
    summary: str | None = None
    confidence: float | None = None
    needs_human: bool = False
    acceptance_satisfied: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    error: str | None = None
    policy_denied: bool = False
    tool_failures: dict[str, int] = field(default_factory=dict)
    artifacts: dict[str, object] = field(default_factory=dict)

    #: Per-run token/cost aggregation (HARD-02) and the real model id the
    #: provider reported (used to price the run); ``None`` until a response
    #: carries a model.
    usage: UsageAccumulator = field(default_factory=UsageAccumulator)
    model_name: str | None = None

    def add_step(self, step: Step) -> Step:
        step.index = len(self.steps)
        self.steps.append(step)
        return step
