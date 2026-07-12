"""Counterfactual-fork wrappers for the two nondeterministic boundaries.

Where :mod:`~forge_agent.replay.player` replays a whole run by substitution, a
*fork* replays it only up to a chosen ``fork_index`` and then lets it diverge:
the pre-fork calls come back byte-for-byte off the tape (guarded by the same
divergence canary as replay), but every call at/after the fork is executed
*live* — the LLM boundary against a **different** ``ModelClient`` (a new model /
prompt, built via the per-role ``model_client_factory`` seam) and the tool
boundary against the real :class:`~forge_agent.tools.ToolRegistry`. This is the
"what if the agent had used a different model from step N?" question.

Both boundaries are indexed independently (each replayed by its own call-index),
and the interleaving of the plan -> act -> observe loop keeps them aligned: LLM
call ``i`` is followed by tool call ``i``, so a single ``fork_index`` applied to
each boundary forks the run at the same logical point.

Because :class:`~forge_agent.runtime.AgentRunner` injects both the model client
and the tool registry through its constructor, forking needs *no* change to
``runtime.py``::

    runner = AgentRunner(
        model=ForkModelClient(cassette, fork_index, new_client),
        tools=ForkToolRegistry(cassette, fork_index, live_tools),
    )
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from forge_agent.replay.cassette import RunCassette, args_digest, request_digest
from forge_agent.replay.player import ReplayDivergenceError
from forge_agent.tools import ToolRegistry, ToolResult
from forge_contracts import ModelClient, ModelRequest, ModelResponse, ModelStreamEvent

__all__ = ["ForkModelClient", "ForkToolRegistry"]


class ForkModelClient:
    """A :class:`~forge_contracts.ModelClient` that replays, then delegates.

    For each :meth:`complete` call whose index is ``< fork_index`` the recorded
    :class:`~forge_contracts.ModelResponse` is returned by substitution (guarded
    by the divergence canary — the incoming request digest must match the tape,
    exactly like :class:`~forge_agent.replay.player.ReplayModelClient`). From
    ``fork_index`` on, the call is delegated to ``new_client`` — a *different*
    model / prompt — so the run diverges from the recording on purpose.
    """

    def __init__(
        self,
        cassette: RunCassette,
        fork_index: int,
        new_client: ModelClient,
    ) -> None:
        self._cassette = cassette
        self._fork_index = max(0, fork_index)
        self._new = new_client
        self._index = 0

    @property
    def index(self) -> int:
        """How many completions have been served (replayed + delegated)."""
        return self._index

    @property
    def fork_index(self) -> int:
        """The call-index at which replay stops and delegation begins."""
        return self._fork_index

    def complete(self, request: ModelRequest) -> ModelResponse:
        index = self._index
        if index < self._fork_index:
            entry = (
                self._cassette.llm_calls[index] if index < len(self._cassette.llm_calls) else None
            )
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
        # At/after the fork: run live against the new (different) model client.
        self._index += 1
        return self._new.complete(request)

    def stream(self, request: ModelRequest) -> Iterator[ModelStreamEvent]:
        # Mirror the sibling clients: the runtime drives the loop via
        # ``complete``; reconstruct a single text event from the response.
        response = self.complete(request)
        yield ModelStreamEvent(type="text", text=response.content, delta=response.content)


class ForkToolRegistry:
    """A :class:`~forge_agent.tools.ToolRegistry` facade that replays, then runs live.

    For each :meth:`dispatch` whose index is ``< fork_index`` the recorded
    :class:`~forge_agent.tools.ToolResult` is returned by substitution (guarded
    by the args-digest divergence canary). From ``fork_index`` on, the dispatch
    is executed against the real ``tools`` registry — the counterfactual run
    takes real actions after the fork. Every other attribute (``schemas``,
    ``action_for``, ``has`` …) delegates to ``tools`` so the runtime advertises
    the same tool schemas it did while recording; without matching schemas the
    pre-fork LLM-boundary digests would spuriously diverge.
    """

    def __init__(
        self,
        cassette: RunCassette,
        fork_index: int,
        tools: ToolRegistry | None = None,
    ) -> None:
        self._cassette = cassette
        self._fork_index = max(0, fork_index)
        self._inner = tools if tools is not None else ToolRegistry()
        self._index = 0

    @property
    def index(self) -> int:
        """How many dispatches have been served (replayed + executed live)."""
        return self._index

    @property
    def fork_index(self) -> int:
        """The call-index at which replay stops and live execution begins."""
        return self._fork_index

    def dispatch(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        index = self._index
        if index < self._fork_index:
            entry = (
                self._cassette.tool_calls[index] if index < len(self._cassette.tool_calls) else None
            )
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
        # At/after the fork: run the real tool.
        self._index += 1
        return self._inner.dispatch(name, arguments)

    def __getattr__(self, item: str) -> Any:
        # Delegate everything not defined here (schemas/action_for/has/…) to the
        # inner registry. ``__getattr__`` only fires for genuinely missing
        # attributes, so the explicit ``dispatch`` above always wins.
        return getattr(self._inner, item)
