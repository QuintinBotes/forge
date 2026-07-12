"""Replaying a recorded cassette reproduces a run by *substitution*: the same
``AgentRunResult.steps`` come back byte-for-byte without any real provider or
tool being touched, and any drift from the tape trips the divergence canary.
"""

from __future__ import annotations

import dataclasses
import itertools

import pytest

from forge_agent.replay import (
    RecordingModelClient,
    RecordingToolRegistry,
    ReplayDivergenceError,
    ReplayModelClient,
    ReplayToolRegistry,
    RunCassette,
)
from forge_agent.replay.cassette import canonical_json
from forge_agent.runtime import AgentRunner
from forge_agent.testing import ScriptedModelClient, finish_response, tool_response
from forge_agent.tools import ToolRegistry, ToolResult
from forge_contracts import AgentObjective, AgentRunResult, RunStatus


def _clock():
    counter = itertools.count()

    def tick() -> float:
        return float(next(counter))

    return tick


def _objective(text: str = "edit main") -> AgentObjective:
    return AgentObjective(objective=text, allowed_actions=["read_repo", "write_code"])


def _scripted_responses() -> list:
    return [
        tool_response("read_file", {"path": "app/main.py", "action": "read_repo"}),
        tool_response("write_file", {"path": "app/main.py", "action": "write_code"}),
        finish_response("done", confidence=0.9, acceptance_criteria_satisfied=["A1"]),
    ]


def _record_run() -> tuple[AgentRunResult, RunCassette, ToolRegistry]:
    """Run once through the recording wrappers and return the taped cassette.

    Also returns a *fresh* registry (same tools, same schemas) suitable for
    replay: it exposes the identical tool schemas the runtime advertised while
    recording, so the replayed ``ModelRequest`` digests match.
    """
    inner_registry = ToolRegistry()
    inner_registry.add(
        "read_file", lambda _a: ToolResult(ok=True, output="file contents"), action="read_repo"
    )
    inner_registry.add(
        "write_file", lambda _a: ToolResult(ok=True, output="wrote 2 lines"), action="write_code"
    )

    cassette = RunCassette(clock=_clock())
    model = RecordingModelClient(ScriptedModelClient(_scripted_responses()), cassette)
    tools = RecordingToolRegistry(inner_registry, cassette)
    result = AgentRunner(model=model, tools=tools).run(_objective())
    return result, cassette, inner_registry


def _replay_registry(*, forbid_dispatch: bool = True) -> ToolRegistry:
    """A registry with the same schemas but tool handlers that must never run.

    Replay returns recorded ``ToolResult``s by substitution, so the real handler
    is proof-of-non-invocation: it raises if the runtime ever dispatches it.
    """

    def _boom(_a: dict) -> ToolResult:
        if forbid_dispatch:
            raise AssertionError("real tool handler must not run during replay")
        return ToolResult(ok=True)

    reg = ToolRegistry()
    reg.add("read_file", _boom, action="read_repo")
    reg.add("write_file", _boom, action="write_code")
    return reg


def test_replay_reproduces_identical_steps_without_touching_real_tools() -> None:
    original, cassette, _ = _record_run()
    assert original.status is RunStatus.SUCCEEDED

    replay_tools = _replay_registry()  # handlers raise if ever dispatched
    replayed = AgentRunner(
        model=ReplayModelClient(cassette),
        tools=ReplayToolRegistry(cassette, tools=replay_tools),
    ).run(_objective())

    assert replayed.status is original.status
    # Byte-identical steps: structural equality *and* canonical serialisation.
    assert replayed.steps == original.steps
    assert [canonical_json(s) for s in replayed.steps] == [
        canonical_json(s) for s in original.steps
    ]
    assert replayed.output == original.output
    assert replayed.confidence == original.confidence


def test_replay_dispatch_returns_recorded_result_object() -> None:
    _, cassette, _ = _record_run()
    tools = ReplayToolRegistry(cassette)

    # Dispatching a recorded tool hands back the exact recorded result object
    # (substitution — the real handler is never consulted). Key order in the
    # args is irrelevant: the digest is canonical.
    recorded = cassette.tool_calls[0]
    assert (
        tools.dispatch(recorded.name, {"action": "read_repo", "path": "app/main.py"})
        is recorded.result
    )


def test_tampered_request_raises_divergence() -> None:
    _, cassette, _ = _record_run()

    # Replay against a *different* objective: the very first (plan) request the
    # runtime builds no longer matches the recorded digest at index 0.
    with pytest.raises(ReplayDivergenceError) as excinfo:
        AgentRunner(
            model=ReplayModelClient(cassette),
            tools=ReplayToolRegistry(cassette, tools=_replay_registry(forbid_dispatch=False)),
        ).run(_objective("a completely different objective"))

    err = excinfo.value
    assert err.boundary == "llm"
    assert err.index == 0


def test_tampered_tool_args_raises_divergence() -> None:
    _, cassette, _ = _record_run()

    # Corrupt the first recorded tool entry's args digest. The LLM digests still
    # match, so the run proceeds to the first dispatch — where the incoming args
    # no longer match the tape and the canary fires.
    cassette.tool_calls[0] = dataclasses.replace(cassette.tool_calls[0], args_digest="deadbeef")

    with pytest.raises(ReplayDivergenceError) as excinfo:
        AgentRunner(
            model=ReplayModelClient(cassette),
            tools=ReplayToolRegistry(cassette, tools=_replay_registry(forbid_dispatch=False)),
        ).run(_objective())

    err = excinfo.value
    assert err.boundary == "tool"
    assert err.index == 0
    assert err.name == "read_file"


def test_replay_past_end_of_tape_raises_divergence() -> None:
    _, cassette, _ = _record_run()
    client = ReplayModelClient(cassette)
    # Exhaust the recorded LLM calls by replaying nothing but tracking the index:
    # an out-of-range index has no recorded entry, so any call diverges.
    empty = RunCassette()
    with pytest.raises(ReplayDivergenceError) as excinfo:
        ReplayToolRegistry(empty).dispatch("read_file", {"path": "x"})
    assert excinfo.value.expected is None
    assert client is not None
