"""Run token budgets stop the run before the next model call (ADR-0008)."""

from __future__ import annotations

import pytest

from forge_agent import AgentRunner
from forge_agent.budget import BudgetedModelClient
from forge_agent.providers.base import ModelClientError
from forge_agent.testing import ScriptedModelClient, tool_response
from forge_agent.tools import ToolRegistry, ToolResult
from forge_contracts import AgentObjective, ModelRequest, ModelResponse, RunStatus, TokenUsage


class Metered:
    """Reports 60 tokens per call."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return ModelResponse(content="ok", usage=TokenUsage(input_tokens=50, output_tokens=10))

    def stream(self, request: ModelRequest):  # type: ignore[no-untyped-def]
        return iter(())


def test_budget_is_checked_before_each_call() -> None:
    inner = Metered()
    client = BudgetedModelClient(inner, max_tokens=100)
    request = ModelRequest(model="m")
    assert client.complete(request).stop_reason is None
    assert client.complete(request).stop_reason is None
    stopped = client.complete(request)
    assert stopped.stop_reason == "budget_exhausted"
    assert inner.calls == 2, "an exhausted budget never reaches the provider"
    assert client.used_tokens == 120 and not client.estimated


def test_unmetered_providers_are_charged_an_estimate() -> None:
    unmetered = ScriptedModelClient(responses=[ModelResponse(content="x" * 400)])
    client = BudgetedModelClient(unmetered, max_tokens=50)
    client.complete(ModelRequest(model="m"))
    assert client.estimated and client.used_tokens == 100
    assert client.exhausted


def test_exhausted_stream_raises() -> None:
    client = BudgetedModelClient(Metered(), max_tokens=1)
    client.complete(ModelRequest(model="m"))
    with pytest.raises(ModelClientError, match="budget"):
        client.stream(ModelRequest(model="m"))


def test_nonpositive_budget_is_rejected() -> None:
    with pytest.raises(ValueError):
        BudgetedModelClient(Metered(), max_tokens=0)


def test_a_looping_run_fails_at_its_budget() -> None:
    tools = ToolRegistry()
    tools.add("noop", lambda _: ToolResult(ok=True, output="y" * 2000))
    looping = ScriptedModelClient(responses=[], default=tool_response("noop"))
    runner = AgentRunner(BudgetedModelClient(looping, max_tokens=1_000), tools=tools)
    result = runner.run(AgentObjective(objective="loop forever"))
    assert result.status == RunStatus.FAILED
    assert "budget_exhausted" in (result.error or "")
