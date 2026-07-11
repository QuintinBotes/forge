"""Replay-by-substitution wrappers for the two nondeterministic boundaries.

Where :mod:`~forge_agent.replay.recorder` *captures* a run onto a
:class:`~forge_agent.replay.cassette.RunCassette`, this module *replays* one: the
wrappers here never touch a real provider or tool. On each call they return the
:class:`~forge_contracts.ModelResponse` / :class:`~forge_agent.tools.ToolResult`
the cassette recorded at the current call-index.

Determinism note: the target providers 400 on ``seed``/``temperature`` (see
``providers/translate.py``), so replay can never be by re-seeding the model — it
is by **substitution**. The correctness net is the divergence canary: on each
call we recompute the incoming request/args digest and compare it against the
recorded digest at that index. A mismatch — the replay input drifted from what
was taped — raises :class:`ReplayDivergenceError` (mirroring Temporal's
replay-divergence check) rather than silently returning a stale value.

Because :class:`~forge_agent.runtime.AgentRunner` injects both the model client
and the tool registry through its constructor, replay needs *no* change to
``runtime.py``::

    runner = AgentRunner(
        model=ReplayModelClient(cassette),
        tools=ReplayToolRegistry(cassette, tools=registry),
    )
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from forge_agent.replay.cassette import RunCassette, args_digest, request_digest
from forge_agent.tools import ToolRegistry, ToolResult
from forge_contracts import ModelRequest, ModelResponse, ModelStreamEvent

__all__ = ["ReplayDivergenceError", "ReplayModelClient", "ReplayToolRegistry"]


class ReplayDivergenceError(RuntimeError):
    """Raised when a replayed call diverges from the recorded cassette.

    Divergence means the replay is no longer a faithful reproduction of the
    recorded run: either the incoming request/args digest does not match the
    entry recorded at that call-index, or the cassette has no entry for that
    index (the run made more calls than were taped).
    """

    def __init__(
        self,
        *,
        boundary: str,
        index: int,
        expected: str | None,
        actual: str,
        name: str | None = None,
    ) -> None:
        self.boundary = boundary
        self.index = index
        self.expected = expected
        self.actual = actual
        self.name = name
        where = f"{boundary} call #{index}"
        if name is not None:
            where += f" ({name!r})"
        if expected is None:
            detail = "no recorded entry at that index (replay ran past the tape)"
        else:
            detail = f"digest mismatch (recorded {expected}, replayed {actual})"
        super().__init__(f"replay divergence at {where}: {detail}")


class ReplayModelClient:
    """A :class:`~forge_contracts.ModelClient` that replays recorded completions.

    Each :meth:`complete` returns the ``ModelResponse`` recorded at the current
    call-index — no provider is contacted. The incoming request's digest is
    checked against the recorded one; a mismatch raises
    :class:`ReplayDivergenceError`.
    """

    def __init__(self, cassette: RunCassette) -> None:
        self._cassette = cassette
        self._index = 0

    def complete(self, request: ModelRequest) -> ModelResponse:
        index = self._index
        entry = self._cassette.llm_calls[index] if index < len(self._cassette.llm_calls) else None
        actual = request_digest(request)
        if entry is None or actual != entry.request_digest:
            raise ReplayDivergenceError(
                boundary="llm",
                index=index,
                expected=entry.request_digest if entry is not None else None,
                actual=actual,
            )
        self._index += 1
        return entry.response

    def stream(self, request: ModelRequest) -> Iterator[ModelStreamEvent]:
        # Mirror ``ScriptedModelClient``/``RecordingModelClient``: the runtime
        # drives the loop via ``complete``; reconstruct a single text event.
        response = self.complete(request)
        yield ModelStreamEvent(type="text", text=response.content, delta=response.content)


class ReplayToolRegistry:
    """A :class:`~forge_agent.tools.ToolRegistry` facade that replays dispatches.

    Only :meth:`dispatch` is intercepted: it returns the ``ToolResult`` recorded
    at the current call-index (the real tool is never run), guarded by an
    args-digest divergence check. Every other attribute — ``schemas``,
    ``action_for``, ``has`` … — delegates to ``tools`` so the runtime builds the
    *same* ``ModelRequest`` (which advertises the tool schemas) it did while
    recording; without matching schemas the LLM-boundary digest would spuriously
    diverge. ``tools`` defaults to an empty registry.
    """

    def __init__(self, cassette: RunCassette, *, tools: ToolRegistry | None = None) -> None:
        self._cassette = cassette
        self._inner = tools if tools is not None else ToolRegistry()
        self._index = 0

    def dispatch(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        index = self._index
        entry = self._cassette.tool_calls[index] if index < len(self._cassette.tool_calls) else None
        actual = args_digest(arguments)
        if entry is None or entry.name != name or actual != entry.args_digest:
            raise ReplayDivergenceError(
                boundary="tool",
                index=index,
                expected=entry.args_digest if entry is not None else None,
                actual=actual,
                name=name,
            )
        self._index += 1
        return entry.result

    def __getattr__(self, item: str) -> Any:
        # Delegate everything not defined here (schemas/action_for/has/…) to the
        # inner registry. ``__getattr__`` only fires for genuinely missing
        # attributes, so the explicit ``dispatch`` above always wins.
        return getattr(self._inner, item)
