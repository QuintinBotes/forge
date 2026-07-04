"""HARD-02 AC4/AC5: usage accounting + per-run aggregation into ``model_usage``."""

from __future__ import annotations

from collections.abc import Iterator

from forge_agent import AgentRunner
from forge_agent.providers import MODEL_PRICING, UsageAccumulator, cost_usd
from forge_agent.tools import FINISH_TOOL, ToolRegistry, ToolResult
from forge_contracts import (
    AgentObjective,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelToolCall,
    RunStatus,
    TokenUsage,
)

_MODEL = "claude-opus-4-8"


def test_cost_usd_matches_pricing_table() -> None:
    usage = TokenUsage(input_tokens=1000, output_tokens=500)
    in_rate, out_rate = MODEL_PRICING[_MODEL]
    expected = (1000 * in_rate + 500 * out_rate) / 1_000_000
    assert cost_usd(_MODEL, usage) == expected


def test_cost_usd_cache_reads_lower_cost() -> None:
    usage = TokenUsage(input_tokens=1000, output_tokens=500)
    with_cache = cost_usd(_MODEL, usage, cache_read_tokens=4000)
    # Cache reads are billed extra-but-cheap; still strictly less than pricing
    # the same 4000 tokens at the full input rate would cost.
    in_rate, _ = MODEL_PRICING[_MODEL]
    full = cost_usd(_MODEL, usage) + 4000 * in_rate / 1_000_000
    assert cost_usd(_MODEL, usage) < with_cache < full


def test_unknown_model_costs_zero() -> None:
    assert cost_usd("forge-fake-model", TokenUsage(input_tokens=9, output_tokens=9)) == 0.0


def test_accumulator_single_turn_artifact() -> None:
    acc = UsageAccumulator()
    acc.add(TokenUsage(input_tokens=42, output_tokens=17))
    artifact = acc.to_artifact(_MODEL)
    assert artifact["input_tokens"] == 42
    assert artifact["output_tokens"] == 17
    assert artifact["calls"] == 1
    assert artifact["cost_usd"] == cost_usd(_MODEL, TokenUsage(input_tokens=42, output_tokens=17))


class _UsageClient:
    """A fake ``ModelClient`` that returns a scripted response (with usage) per turn."""

    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = responses
        self._index = 0

    def complete(self, request: ModelRequest) -> ModelResponse:
        response = self._responses[self._index]
        self._index += 1
        return response

    def stream(self, request: ModelRequest) -> Iterator[ModelStreamEvent]:  # pragma: no cover
        yield ModelStreamEvent(type="text", text="", delta="")


def test_per_run_aggregation_sums_tokens_and_counts_turns() -> None:
    registry = ToolRegistry()
    registry.add("echo", lambda _a: ToolResult(ok=True, output="echoed"))
    client = _UsageClient(
        [
            ModelResponse(
                stop_reason="tool_use",
                model=_MODEL,
                tool_calls=[ModelToolCall(name="echo", arguments={})],
                usage=TokenUsage(input_tokens=10, output_tokens=5),
            ),
            ModelResponse(
                stop_reason="tool_use",
                model=_MODEL,
                tool_calls=[
                    ModelToolCall(name=FINISH_TOOL, arguments={"output": "done", "confidence": 0.9})
                ],
                usage=TokenUsage(input_tokens=20, output_tokens=7),
            ),
        ]
    )
    runner = AgentRunner(client, tools=registry)
    result = runner.run(AgentObjective(objective="aggregate usage"))

    assert result.status == RunStatus.SUCCEEDED
    usage = result.artifacts["model_usage"]
    assert usage["input_tokens"] == 30
    assert usage["output_tokens"] == 12
    assert usage["calls"] == 2  # one model turn per plan/observe call
    assert usage["cost_usd"] == cost_usd(_MODEL, TokenUsage(input_tokens=30, output_tokens=12))
