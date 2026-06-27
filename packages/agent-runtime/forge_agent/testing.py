"""Deterministic test doubles for the agent runtime.

These are importable helpers (not fixtures) so other packages and the API/worker
layer can drive the runtime without a live model provider. The plan requires
"Model calls via ``ModelClient`` (fake for tests)".
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from forge_agent.tools import FINISH_TOOL
from forge_contracts import (
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelToolCall,
    TokenUsage,
)

__all__ = ["ScriptedModelClient", "finish_response", "tool_response"]


def tool_response(name: str, arguments: dict[str, Any] | None = None) -> ModelResponse:
    """A model response that requests a single tool call."""
    return ModelResponse(
        content="",
        stop_reason="tool_use",
        tool_calls=[ModelToolCall(name=name, arguments=arguments or {})],
    )


def finish_response(
    output: str,
    *,
    confidence: float | None = None,
    acceptance_criteria_satisfied: list[str] | None = None,
    needs_human: bool = False,
    risks: list[str] | None = None,
    summary: str | None = None,
) -> ModelResponse:
    """A model response that calls the ``finish`` control tool."""
    arguments: dict[str, Any] = {"output": output}
    if summary is not None:
        arguments["summary"] = summary
    if confidence is not None:
        arguments["confidence"] = confidence
    if acceptance_criteria_satisfied is not None:
        arguments["acceptance_criteria_satisfied"] = acceptance_criteria_satisfied
    if needs_human:
        arguments["needs_human"] = True
    if risks is not None:
        arguments["risks"] = risks
    return ModelResponse(
        content="",
        stop_reason="tool_use",
        tool_calls=[ModelToolCall(name=FINISH_TOOL, arguments=arguments)],
    )


class ScriptedModelClient:
    """A :class:`~forge_contracts.ModelClient` that replays scripted responses.

    Each :meth:`complete` call returns the next scripted response and records the
    request it received (so tests can assert on the system prompt / messages). If
    the script is exhausted, ``default`` is returned, or an empty stop response.
    """

    def __init__(
        self,
        responses: list[ModelResponse],
        *,
        default: ModelResponse | None = None,
    ) -> None:
        self._responses = list(responses)
        self._index = 0
        self._default = default
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self._index < len(self._responses):
            response = self._responses[self._index]
            self._index += 1
            return response
        if self._default is not None:
            return self._default
        return ModelResponse(content="", stop_reason="end_turn", usage=TokenUsage())

    def stream(self, request: ModelRequest) -> Iterator[ModelStreamEvent]:
        response = self.complete(request)
        yield ModelStreamEvent(type="text", text=response.content, delta=response.content)
