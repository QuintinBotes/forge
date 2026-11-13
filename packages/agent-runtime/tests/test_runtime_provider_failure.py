"""A provider failure reported as a ``stop_reason`` fails the run (spec F §3, AC6).

The SDK-backed providers signal a failed turn by raising ``ModelClientError``.
The local-CLI providers cannot — their ``complete()`` is contractually
never-raising — so they return a ``ModelResponse`` with a diagnostic
``stop_reason`` and no content and no tool calls. Before this was handled, such
a response fell through the runtime's "the model finished with no tool use"
branch and produced a ``SUCCEEDED`` run with an empty output: a malformed or
unreachable CLI looked exactly like a completed objective.

These tests pin the whole vocabulary (``PROVIDER_FAILURE_STOP_REASONS``, the
single definition the providers emit from) against the runtime, and pin that
the ordinary stop reasons around it are untouched.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from forge_agent import AgentRunner
from forge_agent.runtime import _is_provider_failure, _is_refusal
from forge_contracts import (
    PROVIDER_FAILURE_STOP_REASONS,
    AgentObjective,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelToolCall,
    RunStatus,
    TokenUsage,
)

#: Spec F §7's table, transcribed by hand rather than imported, so this test
#: fails if the shared set ever drifts from the document it implements.
SPEC_STOP_REASONS = {
    "provider_unavailable",
    "provider_error",
    "timeout",
    "parse_error",
    "auth_error",
    "input_too_large",
    # Spec G / ADR-0008: a run that spends its token budget fails the same way.
    "budget_exhausted",
}


class _FailingClient:
    """A local-CLI-shaped client: returns a diagnostic stop_reason, never raises."""

    def __init__(self, stop_reason: str) -> None:
        self.calls = 0
        self._stop_reason = stop_reason

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            content="",
            model="local-cli:claude",
            stop_reason=self._stop_reason,
            usage=TokenUsage(input_tokens=11, output_tokens=0),
        )

    def stream(self, request: ModelRequest) -> Iterator[ModelStreamEvent]:  # pragma: no cover
        yield ModelStreamEvent(type="text", text="", delta="")


class _FinishingClient:
    """A healthy client: ordinary end_turn with no tool calls."""

    def __init__(self, stop_reason: str | None = "end_turn") -> None:
        self.calls = 0
        self._stop_reason = stop_reason

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            content="all done",
            model="local-cli:claude",
            stop_reason=self._stop_reason,
        )

    def stream(self, request: ModelRequest) -> Iterator[ModelStreamEvent]:  # pragma: no cover
        yield ModelStreamEvent(type="text", text="", delta="")


def test_the_shared_set_is_exactly_the_spec_table() -> None:
    assert PROVIDER_FAILURE_STOP_REASONS == SPEC_STOP_REASONS


@pytest.mark.parametrize("stop_reason", sorted(SPEC_STOP_REASONS))
def test_provider_failure_fails_the_run_and_names_the_reason(stop_reason: str) -> None:
    client = _FailingClient(stop_reason)
    result = AgentRunner(client).run(AgentObjective(objective="do the thing"))

    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert stop_reason in result.error
    # Never an empty success: the output must not be a blank string passed off
    # as the objective's answer.
    assert result.output is None
    assert any(stop_reason in risk for risk in result.artifacts["risks"])
    # One turn only — a failed provider is not retried blindly.
    assert client.calls == 1


@pytest.mark.parametrize("stop_reason", sorted(SPEC_STOP_REASONS))
def test_provider_failure_is_not_reported_as_succeeded(stop_reason: str) -> None:
    """The regression itself: this used to be ``SUCCEEDED`` with ``output == ""``."""
    result = AgentRunner(_FailingClient(stop_reason)).run(AgentObjective(objective="x"))
    assert result.status is not RunStatus.SUCCEEDED
    assert result.output != ""


def test_an_ordinary_finish_still_succeeds() -> None:
    """Guard against an over-broad predicate: healthy turns are untouched."""
    client = _FinishingClient()
    result = AgentRunner(client).run(AgentObjective(objective="do the thing"))
    assert result.status is RunStatus.SUCCEEDED
    assert result.output == "all done"


def test_a_missing_stop_reason_still_succeeds() -> None:
    """``None`` is what the SDK providers report on a plain completion."""
    result = AgentRunner(_FinishingClient(stop_reason=None)).run(AgentObjective(objective="x"))
    assert result.status is RunStatus.SUCCEEDED


@pytest.mark.parametrize(
    "stop_reason",
    [None, "", "end_turn", "tool_use", "max_tokens", "stop", "refusal", "refusal:cyber"],
)
def test_predicate_ignores_everything_outside_the_diagnostic_set(stop_reason: str | None) -> None:
    """Including the refusal vocabulary, which has its own (escalating) handler."""
    assert _is_provider_failure(stop_reason) is False


@pytest.mark.parametrize("stop_reason", sorted(SPEC_STOP_REASONS))
def test_predicates_do_not_overlap(stop_reason: str) -> None:
    assert _is_provider_failure(stop_reason) is True
    assert _is_refusal(stop_reason) is False


def test_tool_calls_alongside_a_failure_are_not_dispatched() -> None:
    """A diagnostic stop wins over any content the failed reply happened to carry."""

    class _Client(_FailingClient):
        def complete(self, request: ModelRequest) -> ModelResponse:
            self.calls += 1
            return ModelResponse(
                content="ignore me",
                model="local-cli:claude",
                stop_reason="parse_error",
                tool_calls=[ModelToolCall(name="read_file", arguments={"path": "a.py"})],
            )

    result = AgentRunner(_Client("parse_error")).run(AgentObjective(objective="x"))
    assert result.status is RunStatus.FAILED
    assert all(step.tool_call is None for step in result.steps)
