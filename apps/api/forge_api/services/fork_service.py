"""Time-Travel Runs counterfactual-fork service — the pure replay-then-diverge
core behind ``POST /agent/runs/{run_id}/fork``.

No DB access lives here (see ``routers/agent.py`` for the ``RunRecording``
lookup and the ``model_client_factory`` wiring): given a reconstructed
:class:`~forge_agent.replay.RunCassette`, the objective that produced it, a
``fork_index`` and a **new** :class:`~forge_contracts.ModelClient`,
:func:`fork_recording` drives a fresh :class:`~forge_agent.AgentRunner` through
the fork wrappers (:class:`~forge_agent.replay.ForkModelClient` /
:class:`~forge_agent.replay.ForkToolRegistry`). The pre-fork prefix is replayed
by substitution (a drift from the tape trips the same divergence canary as a
full replay); from the fork on the run goes live against the new model / real
tools and diverges from the recording on purpose.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import cast

from forge_agent.replay import (
    ForkModelClient,
    ForkToolRegistry,
    ReplayDivergenceError,
    RunCassette,
)
from forge_agent.runtime import AgentRunner
from forge_agent.tools import ToolRegistry
from forge_api.services.replay_service import Boundary, ReplayDivergence
from forge_contracts import (
    AgentObjective,
    AgentRunResult,
    ModelClient,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
)

__all__ = ["ForkOutcome", "PromptOverrideModelClient", "fork_recording"]


class PromptOverrideModelClient:
    """Wrap a :class:`~forge_contracts.ModelClient`, augmenting its system prompt.

    Every completion's :class:`~forge_contracts.ModelRequest` has ``override``
    appended to its system prompt before it is delegated to ``inner``. Used to
    apply a fork's ``prompt_override`` to the post-fork completions only — the
    replayed pre-fork prefix never reaches this wrapper (``ForkModelClient``
    serves those off the tape), so its request digests stay intact.
    """

    def __init__(self, inner: ModelClient, override: str) -> None:
        self._inner = inner
        self._override = override

    def _rewrite(self, request: ModelRequest) -> ModelRequest:
        system = f"{request.system}\n\n{self._override}" if request.system else self._override
        return request.model_copy(update={"system": system})

    def complete(self, request: ModelRequest) -> ModelResponse:
        return self._inner.complete(self._rewrite(request))

    def stream(self, request: ModelRequest) -> Iterator[ModelStreamEvent]:
        return self._inner.stream(self._rewrite(request))


@dataclass(frozen=True)
class ForkOutcome:
    """The result of a counterfactual fork."""

    diverged: bool
    divergence: ReplayDivergence | None
    fork_index: int
    result: AgentRunResult | None


def fork_recording(
    cassette: RunCassette,
    objective: AgentObjective,
    *,
    fork_index: int,
    new_client: ModelClient,
    tools: ToolRegistry | None = None,
    max_iterations: int = 12,
) -> ForkOutcome:
    """Replay ``objective`` up to ``fork_index``, then run live off ``new_client``.

    The pre-fork prefix is answered by the cassette's recorded values at each
    call-index; a :class:`~forge_agent.replay.ReplayDivergenceError` there (the
    objective no longer reproduces the tape) is caught and reported as the
    ``diverged`` signal — the fork aborts with no result. From ``fork_index``
    on, LLM completions run against ``new_client`` (a different model / prompt)
    and tool dispatches run against ``tools`` (the real registry); this
    post-fork divergence from the recording is intentional and is *not*
    reported as a divergence.

    ``tools`` should expose the same tool schemas the recording was taped under
    (``ForkToolRegistry`` delegates ``schemas()``/``action_for()`` to it), or the
    very first replayed ``ModelRequest`` spuriously diverges on its advertised
    tools. Defaults to an empty registry, matching the worker's recording wiring.
    """
    fork_model = ForkModelClient(cassette, fork_index, new_client)
    fork_tools = ForkToolRegistry(cassette, fork_index, tools)
    runner = AgentRunner(
        model=fork_model,
        tools=cast(ToolRegistry, fork_tools),
        max_iterations=max_iterations,
    )

    divergence: ReplayDivergence | None = None
    result: AgentRunResult | None = None
    try:
        result = runner.run(objective)
    except ReplayDivergenceError as exc:
        divergence = ReplayDivergence(
            boundary=cast(Boundary, exc.boundary),
            index=exc.index,
            name=exc.name,
            expected=exc.expected,
            actual=exc.actual,
        )

    return ForkOutcome(
        diverged=divergence is not None,
        divergence=divergence,
        fork_index=fork_index,
        result=result,
    )
