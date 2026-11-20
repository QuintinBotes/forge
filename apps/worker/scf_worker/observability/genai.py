"""GenAI spans around the agent loop's two boundaries (#213).

Like the Time-Travel Runs recording wrappers, these wrap the model client and the
tool registry from the outside, so ``scf_agent`` needs no change and no
dependency on ``scf_obs``. Applied outermost, a replay wrapped the same way yields
the same span tree as the live run. The policy gate decides before ``dispatch``,
so a denied tool call never reaches the registry and gets no span.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from scf_agent.providers.pricing import MODEL_PRICING, cost_usd
from scf_agent.tools import ToolRegistry, ToolResult
from scf_contracts import (
    PROVIDER_FAILURE_STOP_REASONS,
    ModelClient,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
)
from scf_obs.genai import inference_span, record_error, record_inference, tool_span

__all__ = ["TracedModelClient", "TracedToolRegistry"]


class TracedModelClient:
    """A ``ModelClient`` that opens one inference span per ``complete`` call."""

    def __init__(self, inner: ModelClient, *, provider: str, model: str | None) -> None:
        self._inner = inner
        self._provider = provider
        #: The model the client runs when a request pins none.
        self._model = model

    def complete(self, request: ModelRequest) -> ModelResponse:
        requested = request.model or self._model
        with inference_span(provider=self._provider, model=requested) as span:
            response = self._inner.complete(request)
            # Priced like the run's ``model_usage`` artifact, by the reported model.
            priced = response.model or requested
            cost = cost_usd(priced, response.usage) if priced and priced in MODEL_PRICING else None
            record_inference(
                span, response_model=response.model, usage=response.usage, cost_usd=cost
            )
            if response.stop_reason in PROVIDER_FAILURE_STOP_REASONS:
                record_error(span, str(response.stop_reason))
        return response

    def stream(self, request: ModelRequest) -> Iterator[ModelStreamEvent]:
        # The runtime drives the loop through ``complete``.
        return self._inner.stream(request)


class TracedToolRegistry:
    """A ``ToolRegistry`` facade that opens one tool span per dispatched call."""

    def __init__(self, inner: ToolRegistry) -> None:
        self._inner = inner

    def dispatch(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        # A name the model invented is model output: never a span name.
        with tool_span(name if self._inner.has(name) else "unknown_tool") as span:
            result = self._inner.dispatch(name, arguments)
            if not result.ok:
                record_error(span, "tool_error")
        return result

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)
