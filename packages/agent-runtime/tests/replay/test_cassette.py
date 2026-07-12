"""``RunCassette.from_dict`` — the inverse of ``to_dict`` — reconstructs a real
``ModelResponse``/``ToolResult`` cassette from its JSON-native persisted form,
so a ``RunRecording.cassette`` row (the DB substrate) can drive a Time-Travel
Runs replay.
"""

from __future__ import annotations

from forge_agent.replay import (
    RecordingModelClient,
    RecordingToolRegistry,
    ReplayModelClient,
    ReplayToolRegistry,
    RunCassette,
)
from forge_agent.replay.cassette import canonical_json
from forge_agent.runtime import AgentRunner
from forge_agent.testing import ScriptedModelClient, finish_response, tool_response
from forge_agent.tools import ToolRegistry, ToolResult
from forge_contracts import AgentObjective, AgentRunResult, RunStatus


def _objective() -> AgentObjective:
    return AgentObjective(objective="edit main", allowed_actions=["read_repo"])


def _tool_registry() -> ToolRegistry:
    """The tool schemas the recorded run advertised (needed for replay parity).

    Replay needs a registry exposing the *same* tool schemas the recording did
    (``ReplayToolRegistry`` delegates ``schemas()``/``action_for()`` to it) so
    the reconstructed ``ModelRequest`` — and thus its digest — matches; the
    handler itself is never invoked (``ReplayToolRegistry.dispatch`` always
    substitutes the recorded result), so it need not be the same instance.
    """
    registry = ToolRegistry()
    registry.add("read_file", lambda _a: ToolResult(ok=True, output="contents"), action="read_repo")
    return registry


def _record_run() -> tuple[RunCassette, AgentRunResult]:
    cassette = RunCassette()
    model = RecordingModelClient(
        ScriptedModelClient(
            [
                tool_response("read_file", {"path": "x", "action": "read_repo"}),
                finish_response("done", confidence=0.9),
            ]
        ),
        cassette,
    )
    tools = RecordingToolRegistry(_tool_registry(), cassette)
    result = AgentRunner(model=model, tools=tools).run(_objective())
    return cassette, result


def test_from_dict_round_trips_to_dict() -> None:
    cassette, _ = _record_run()
    restored = RunCassette.from_dict(cassette.to_dict())
    assert canonical_json(restored.to_dict()) == canonical_json(cassette.to_dict())


def test_restored_cassette_replays_byte_identical_steps() -> None:
    cassette, original = _record_run()
    restored = RunCassette.from_dict(cassette.to_dict())

    replayed = AgentRunner(
        model=ReplayModelClient(restored),
        tools=ReplayToolRegistry(restored, tools=_tool_registry()),
    ).run(_objective())

    assert replayed.status is RunStatus.SUCCEEDED
    assert [canonical_json(s) for s in replayed.steps] == [
        canonical_json(s) for s in original.steps
    ]
    assert replayed.output == original.output


def test_from_dict_drops_persistence_only_fields_from_tool_result() -> None:
    data = {
        "llm_calls": [],
        "tool_calls": [
            {
                "index": 0,
                "name": "read_file",
                "args_digest": "deadbeef",
                "result": {
                    "ok": True,
                    "output": "trunc",
                    "error": None,
                    "data": {},
                    # Added only at persistence time (see
                    # ``_shape_cassette_for_persistence``); not a ``ToolResult``
                    # field and must not blow up reconstruction.
                    "output_artifact_ref": "artifact://blah",
                },
                "ts": 1.0,
            }
        ],
        "env": {},
    }
    restored = RunCassette.from_dict(data)
    assert restored.tool_calls[0].result == ToolResult(ok=True, output="trunc")


def test_from_dict_handles_empty_cassette() -> None:
    restored = RunCassette.from_dict({})
    assert restored.llm_calls == []
    assert restored.tool_calls == []
    assert restored.env == {}
