"""Counterfactual forks: replay a run up to ``fork_index``, then diverge.

Pre-fork calls come back byte-for-byte off the tape (guarded by the same
divergence canary as a full replay); at/after the fork the LLM boundary runs
against a *different* model client and the tool boundary runs the real tool.
"""

from __future__ import annotations

import dataclasses
import itertools

import pytest

from forge_agent.replay import (
    ForkModelClient,
    ForkToolRegistry,
    RecordingModelClient,
    RecordingToolRegistry,
    ReplayDivergenceError,
    RunCassette,
)
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
        finish_response("original done", confidence=0.9, acceptance_criteria_satisfied=["A1"]),
    ]


def _recording_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.add("read_file", lambda _a: ToolResult(ok=True, output="file contents"), action="read_repo")
    reg.add(
        "write_file", lambda _a: ToolResult(ok=True, output="wrote 2 lines"), action="write_code"
    )
    return reg


def _record_run() -> tuple[AgentRunResult, RunCassette, ToolRegistry]:
    """Tape one run and return (result, cassette, a fresh same-schema registry)."""
    cassette = RunCassette(clock=_clock())
    model = RecordingModelClient(ScriptedModelClient(_scripted_responses()), cassette)
    tools = RecordingToolRegistry(_recording_registry(), cassette)
    result = AgentRunner(model=model, tools=tools).run(_objective())
    return result, cassette, _recording_registry()


def test_fork_index_zero_ignores_the_tape_and_uses_the_new_client() -> None:
    _, cassette, _ = _record_run()

    # fork_index=0 -> nothing is replayed; the new client drives from the very
    # first (plan) call. It finishes immediately with its own output.
    new_client = ScriptedModelClient([finish_response("forked from step 0", confidence=0.8)])
    forked = AgentRunner(
        model=ForkModelClient(cassette, 0, new_client),
        tools=ForkToolRegistry(cassette, 0, ToolRegistry()),
    ).run(_objective())

    assert forked.status is RunStatus.SUCCEEDED
    assert forked.output == "forked from step 0"
    assert forked.confidence == 0.8
    # The new client saw the plan request; nothing was replayed off the tape.
    assert len(new_client.requests) == 1


def test_pre_fork_calls_are_replayed_then_new_client_takes_over() -> None:
    original, cassette, live_tools = _record_run()
    assert original.output == "original done"

    # Fork at LLM call #2 (the observe/finish turn): the plan turn (llm 0), the
    # read_file dispatch (tool 0), the second plan turn (llm 1) and the
    # write_file dispatch (tool 1) all replay off the tape; only the final
    # observe turn is served by the new client — which finishes differently.
    new_client = ScriptedModelClient(
        [finish_response("forked conclusion", confidence=0.8, acceptance_criteria_satisfied=["B2"])]
    )
    forked = AgentRunner(
        model=ForkModelClient(cassette, 2, new_client),
        tools=ForkToolRegistry(cassette, 2, live_tools),
    ).run(_objective())

    assert forked.status is RunStatus.SUCCEEDED
    # Post-fork the run diverged: the new client's output/confidence win.
    assert forked.output == "forked conclusion"
    assert forked.confidence == 0.8
    assert forked.acceptance_criteria_satisfied == ["B2"]
    # Exactly one completion was delegated to the new client (the fork point);
    # everything before it came off the tape.
    assert len(new_client.requests) == 1

    # The replayed prefix is byte-identical to the recording: the same first
    # steps (plan, both tool dispatches) appear in the forked run's trace.
    original_prefix = [s for s in original.steps if s.kind.value in {"decision", "tool_call"}]
    forked_prefix = [s for s in forked.steps if s.kind.value in {"decision", "tool_call"}]
    assert forked_prefix == original_prefix


def test_post_fork_tools_run_live_not_replayed() -> None:
    _, cassette, _ = _record_run()

    # Fork before any tool call: the live registry's handlers must actually run
    # after the fork (proof the tool boundary is live, not substituted).
    executed: list[str] = []

    def _live(name: str):
        def _handler(_a: dict) -> ToolResult:
            executed.append(name)
            return ToolResult(ok=True, output=f"live {name}")

        return _handler

    live = ToolRegistry()
    live.add("read_file", _live("read_file"), action="read_repo")
    live.add("write_file", _live("write_file"), action="write_code")

    # New client re-issues the same tool call the tape had at index 0, then
    # finishes — so the fork exercises a live dispatch.
    new_client = ScriptedModelClient(
        [
            tool_response("read_file", {"path": "app/main.py", "action": "read_repo"}),
            finish_response("forked live", confidence=0.8),
        ]
    )
    forked = AgentRunner(
        model=ForkModelClient(cassette, 0, new_client),
        tools=ForkToolRegistry(cassette, 0, live),
    ).run(_objective())

    assert forked.status is RunStatus.SUCCEEDED
    assert forked.output == "forked live"
    assert executed == ["read_file"]  # the real handler ran post-fork


def test_pre_fork_divergence_still_trips_the_canary() -> None:
    _, cassette, live_tools = _record_run()

    # Corrupt the recorded plan request digest: replaying the pre-fork prefix
    # against the tape now mismatches at llm index 0 and raises.
    cassette.llm_calls[0] = dataclasses.replace(cassette.llm_calls[0], request_digest="deadbeef")

    with pytest.raises(ReplayDivergenceError) as excinfo:
        AgentRunner(
            model=ForkModelClient(cassette, 2, ScriptedModelClient([])),
            tools=ForkToolRegistry(cassette, 2, live_tools),
        ).run(_objective())

    assert excinfo.value.boundary == "llm"
    assert excinfo.value.index == 0


def test_fork_registry_delegates_schemas_to_live_tools() -> None:
    _, cassette, live_tools = _record_run()
    fork_tools = ForkToolRegistry(cassette, 2, live_tools)

    # Schema/introspection attributes delegate to the inner registry (so the
    # runtime advertises the same tools it did while recording).
    assert fork_tools.schemas() == live_tools.schemas()
    assert fork_tools.action_for("read_file") == "read_repo"
    assert fork_tools.fork_index == 2
