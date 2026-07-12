"""Transparent recording wrappers for the two nondeterministic boundaries.

Both wrappers delegate every call to an ``inner`` implementation and append the
call+result to a :class:`~forge_agent.replay.cassette.RunCassette`. Because the
runtime injects the model client and tool registry via its constructor,
``runtime.py`` needs no change to be recorded: wrap the real objects before
handing them to :class:`~forge_agent.runtime.AgentRunner`.

This generalises :class:`~forge_agent.testing.ScriptedModelClient`, which already
records the requests it receives — here we additionally record the *response* and
the tool results, keyed by call-index for replay-by-substitution.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from forge_agent.replay.cassette import RunCassette
from forge_agent.tools import ToolRegistry, ToolResult
from forge_contracts import ModelRequest, ModelResponse, ModelStreamEvent

__all__ = ["RecordingModelClient", "RecordingToolRegistry"]


class RecordingModelClient:
    """A :class:`~forge_contracts.ModelClient` that records every completion.

    Delegates to ``inner`` and appends each ``(request, response)`` pair to
    ``cassette`` in call order.
    """

    def __init__(self, inner: Any, cassette: RunCassette) -> None:
        self._inner = inner
        self._cassette = cassette

    def complete(self, request: ModelRequest) -> ModelResponse:
        response = self._inner.complete(request)
        self._cassette.record_llm(request, response)
        return response

    def stream(self, request: ModelRequest) -> Iterator[ModelStreamEvent]:
        # The runtime drives the loop via ``complete``; mirror
        # ``ScriptedModelClient.stream`` so streaming callers are still recorded
        # (a single text event reconstructed from the completed response).
        response = self.complete(request)
        yield ModelStreamEvent(type="text", text=response.content, delta=response.content)


class RecordingToolRegistry:
    """A :class:`~forge_agent.tools.ToolRegistry` facade that records dispatches.

    Only :meth:`dispatch` is intercepted (to record the call); every other
    attribute — ``schemas``, ``action_for``, ``has``, ``get``, ``names`` … — is
    delegated to ``inner`` unchanged, so the runtime sees an ordinary registry.
    """

    def __init__(self, inner: ToolRegistry, cassette: RunCassette) -> None:
        self._inner = inner
        self._cassette = cassette

    def dispatch(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        result = self._inner.dispatch(name, arguments)
        self._cassette.record_tool(name, arguments, result)
        return result

    def __getattr__(self, item: str) -> Any:
        # Delegate everything not defined here (schemas/action_for/…) to inner.
        # ``__getattr__`` only fires for genuinely missing attributes, so the
        # explicit ``dispatch`` above always wins.
        return getattr(self._inner, item)
