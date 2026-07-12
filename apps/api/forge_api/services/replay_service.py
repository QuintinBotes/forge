"""Time-Travel Runs replay service — the pure re-run-and-diff core behind
``POST /agent/runs/{run_id}/replay``.

No DB access lives here (see ``routers/agent.py`` for the ``RunRecording``
lookup): given a reconstructed :class:`~forge_agent.replay.RunCassette` and
the objective that produced it, :func:`replay_recording` drives a fresh
:class:`~forge_agent.AgentRunner` through the replay-by-substitution wrappers
(``forge_agent.replay.ReplayModelClient`` / ``ReplayToolRegistry`` — never a
live model or tool) and reports, call by call, whether the replay reproduced
the tape and where (if anywhere) it diverged — mirroring the Temporal
replay-divergence pattern this feature is built on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

from forge_agent.replay import (
    ReplayDivergenceError,
    ReplayModelClient,
    ReplayToolRegistry,
    RunCassette,
)
from forge_agent.runtime import AgentRunner
from forge_agent.tools import ToolRegistry
from forge_contracts import AgentObjective, AgentRunResult

__all__ = [
    "ReplayCallDiff",
    "ReplayDivergence",
    "ReplayOutcome",
    "replay_recording",
]

Boundary = Literal["llm", "tool"]


@dataclass(frozen=True)
class ReplayCallDiff:
    """One recorded call vs. its replay outcome."""

    boundary: Boundary
    index: int
    name: str | None
    matched: bool
    recorded_digest: str | None
    replay_digest: str | None


@dataclass(frozen=True)
class ReplayDivergence:
    """Where + why the replay first drifted from the tape."""

    boundary: Boundary
    index: int
    name: str | None
    expected: str | None
    actual: str


@dataclass(frozen=True)
class ReplayOutcome:
    """The step-by-step result of replaying one cassette."""

    diverged: bool
    divergence: ReplayDivergence | None
    steps: list[ReplayCallDiff]
    result: AgentRunResult | None


def replay_recording(
    cassette: RunCassette,
    objective: AgentObjective,
    *,
    tools: ToolRegistry | None = None,
    max_iterations: int = 12,
) -> ReplayOutcome:
    """Re-run ``objective`` through a substitution-replayed ``AgentRunner``.

    Never touches a real model or tool: every LLM/tool call is answered by the
    cassette's recorded value at that call-index. A
    :class:`~forge_agent.replay.ReplayDivergenceError` — the incoming
    request/args no longer matches the tape — is caught here rather than
    propagated: it *is* the "diverged" signal this service reports, not a bug
    in the replay machinery.

    ``tools`` should expose the same tool schemas the recording was taped
    under (``ReplayToolRegistry`` delegates ``schemas()``/``action_for()`` to
    it, exactly like the runtime did while recording) — otherwise the very
    first ``ModelRequest`` the runtime builds spuriously diverges on its
    advertised tools. Defaults to an empty registry, matching
    ``forge_worker.agent_runner.build_agent_runner``'s current recording
    wiring (no tools registered yet there either).
    """
    replay_model = ReplayModelClient(cassette)
    replay_tools = ReplayToolRegistry(cassette, tools=tools)
    runner = AgentRunner(
        model=replay_model,
        tools=cast(ToolRegistry, replay_tools),
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

    steps = _diff_boundary(
        boundary="llm",
        names=[None for _ in cassette.llm_calls],
        digests=[c.request_digest for c in cassette.llm_calls],
        consumed=replay_model.index,
        divergence=divergence,
    ) + _diff_boundary(
        boundary="tool",
        names=[c.name for c in cassette.tool_calls],
        digests=[c.args_digest for c in cassette.tool_calls],
        consumed=replay_tools.index,
        divergence=divergence,
    )

    fully_consumed = replay_model.index == len(cassette.llm_calls) and replay_tools.index == len(
        cassette.tool_calls
    )
    diverged = divergence is not None or not fully_consumed
    return ReplayOutcome(diverged=diverged, divergence=divergence, steps=steps, result=result)


def _diff_boundary(
    *,
    boundary: Boundary,
    names: list[str | None],
    digests: list[str],
    consumed: int,
    divergence: ReplayDivergence | None,
) -> list[ReplayCallDiff]:
    """Per-call diff for one boundary's recorded calls (``consumed`` matched)."""
    diffs: list[ReplayCallDiff] = []
    for index, (name, digest) in enumerate(zip(names, digests, strict=True)):
        if index < consumed:
            # The wrapper only advances its index past a call whose digest
            # matched the tape — a match is the only way to get here.
            diffs.append(
                ReplayCallDiff(
                    boundary=boundary,
                    index=index,
                    name=name,
                    matched=True,
                    recorded_digest=digest,
                    replay_digest=digest,
                )
            )
        elif (
            divergence is not None and divergence.boundary == boundary and divergence.index == index
        ):
            diffs.append(
                ReplayCallDiff(
                    boundary=boundary,
                    index=index,
                    name=name,
                    matched=False,
                    recorded_digest=divergence.expected,
                    replay_digest=divergence.actual,
                )
            )
        else:
            # Never attempted: the replay stopped before reaching this call
            # (it diverged earlier on the other boundary, or simply finished
            # the run without consuming the whole tape).
            diffs.append(
                ReplayCallDiff(
                    boundary=boundary,
                    index=index,
                    name=name,
                    matched=False,
                    recorded_digest=digest,
                    replay_digest=None,
                )
            )
    return diffs
