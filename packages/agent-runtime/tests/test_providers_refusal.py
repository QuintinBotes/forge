"""HARD-02 AC6: a provider ``refusal`` stop escalates to a human, no blind retry."""

from __future__ import annotations

from collections.abc import Iterator

from forge_agent import AgentRunner
from forge_contracts import (
    AgentObjective,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    RunStatus,
    TokenUsage,
)


class _RefusingClient:
    def __init__(self, stop_reason: str = "refusal") -> None:
        self.calls = 0
        self._stop_reason = stop_reason

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            content="",
            model="claude-opus-4-8",
            stop_reason=self._stop_reason,
            usage=TokenUsage(input_tokens=5, output_tokens=0),
        )

    def stream(self, request: ModelRequest) -> Iterator[ModelStreamEvent]:  # pragma: no cover
        yield ModelStreamEvent(type="text", text="", delta="")


def test_refusal_escalates_and_does_not_retry() -> None:
    client = _RefusingClient()
    runner = AgentRunner(client)
    result = runner.run(AgentObjective(objective="please do the flagged thing"))

    assert result.status == RunStatus.ESCALATED
    assert result.needs_human is True
    risks = result.artifacts["risks"]
    assert any("refus" in risk.lower() for risk in risks)
    # No blind retry of a flagged prompt: exactly one model call was made.
    assert client.calls == 1


def test_refusal_category_is_named_in_the_risk() -> None:
    client = _RefusingClient(stop_reason="refusal:cyber")
    runner = AgentRunner(client)
    result = runner.run(AgentObjective(objective="scan"))
    assert result.status == RunStatus.ESCALATED
    assert any("cyber" in risk for risk in result.artifacts["risks"])
    assert client.calls == 1
