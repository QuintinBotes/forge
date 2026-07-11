"""An ``AgentRunner`` wired with the recording wrappers captures every LLM and
tool call, in order, with correct content digests — the raw material a later
slice replays by substitution."""

from __future__ import annotations

import itertools

from forge_agent.replay import (
    RecordingModelClient,
    RecordingToolRegistry,
    RunCassette,
    args_digest,
    request_digest,
)
from forge_agent.replay.cassette import canonical_json
from forge_agent.runtime import AgentRunner
from forge_agent.testing import ScriptedModelClient, finish_response, tool_response
from forge_agent.tools import ToolRegistry, ToolResult
from forge_contracts import AgentObjective, RunStatus


def _clock() -> object:
    counter = itertools.count()

    def tick() -> float:
        return float(next(counter))

    return tick


def _wire() -> tuple[AgentRunner, ScriptedModelClient, RunCassette, list[dict[str, object]]]:
    inner_registry = ToolRegistry()
    seen: list[dict[str, object]] = []

    def read_file(args: dict[str, object]) -> ToolResult:
        seen.append(dict(args))
        return ToolResult(ok=True, output="file contents")

    def write_file(args: dict[str, object]) -> ToolResult:
        seen.append(dict(args))
        return ToolResult(ok=True, output="wrote 2 lines")

    inner_registry.add("read_file", read_file, action="read_repo")
    inner_registry.add("write_file", write_file, action="write_code")

    inner_model = ScriptedModelClient(
        [
            tool_response("read_file", {"path": "app/main.py", "action": "read_repo"}),
            tool_response("write_file", {"path": "app/main.py", "action": "write_code"}),
            finish_response("done", confidence=0.9, acceptance_criteria_satisfied=["A1"]),
        ]
    )

    cassette = RunCassette(clock=_clock())
    model = RecordingModelClient(inner_model, cassette)
    tools = RecordingToolRegistry(inner_registry, cassette)
    runner = AgentRunner(model=model, tools=tools)
    return runner, inner_model, cassette, seen


def test_records_every_llm_and_tool_call_in_order() -> None:
    runner, inner_model, cassette, seen = _wire()
    objective = AgentObjective(
        objective="edit main",
        allowed_actions=["read_repo", "write_code"],
    )

    result = runner.run(objective)

    assert result.status is RunStatus.SUCCEEDED

    # Every completion the runtime made was recorded, in order.
    assert len(cassette.llm_calls) == len(inner_model.requests) == 3
    for i, (entry, request) in enumerate(
        zip(cassette.llm_calls, inner_model.requests, strict=True)
    ):
        assert entry.index == i
        assert entry.request_digest == request_digest(request)
        # The recorded response is exactly what the inner client returned.
        assert entry.response == inner_model._responses[i]

    # Both tool dispatches were recorded, in order, with the dispatched args.
    assert [c.name for c in cassette.tool_calls] == ["read_file", "write_file"]
    assert len(seen) == 2
    for i, (entry, dispatched) in enumerate(zip(cassette.tool_calls, seen, strict=True)):
        assert entry.index == i
        assert entry.args_digest == args_digest(dispatched)
        assert entry.result.ok is True

    # The ``finish`` control tool is handled in the model loop, not dispatched,
    # so it must not appear as a recorded tool call.
    assert all(c.name != "finish" for c in cassette.tool_calls)


def test_recording_tool_registry_delegates_non_dispatch_calls() -> None:
    inner = ToolRegistry()
    inner.add("read_file", lambda _a: ToolResult(ok=True), action="read_repo", description="read")
    wrapped = RecordingToolRegistry(inner, RunCassette())

    # schemas/action_for/has/names are transparently delegated to ``inner``.
    assert wrapped.has("read_file") is True
    assert wrapped.action_for("read_file") == "read_repo"
    assert wrapped.names() == ["read_file"]
    assert wrapped.schemas() == inner.schemas()


def test_recording_model_client_returns_inner_response_unchanged() -> None:
    inner = ScriptedModelClient([tool_response("read_file", {"path": "x"})])
    cassette = RunCassette(clock=_clock())
    client = RecordingModelClient(inner, cassette)

    from forge_contracts import ModelRequest

    request = ModelRequest(model="test-model", system="sys")
    response = client.complete(request)

    assert response is inner._responses[0]
    assert len(cassette.llm_calls) == 1
    assert cassette.llm_calls[0].request_digest == request_digest(request)


def test_digests_are_canonical_and_order_independent() -> None:
    a = {"path": "x", "action": "read_repo"}
    b = {"action": "read_repo", "path": "x"}
    # Same content, different key order -> identical digest.
    assert args_digest(a) == args_digest(b)
    # Canonical json sorts keys.
    assert canonical_json(b) == '{"action":"read_repo","path":"x"}'


def test_with_env_redacts_snapshot_values() -> None:
    class _Redactor:
        def redact(self, text: str) -> str:
            return "REDACTED" if "secret" in text else text

        def register_known_secret(self, value: str) -> None:  # pragma: no cover
            pass

    cassette = RunCassette.with_env(
        {"MODEL": "claude", "API_KEY": "supersecret-token"},
        redactor=_Redactor(),
    )
    assert cassette.env["MODEL"] == "claude"
    assert cassette.env["API_KEY"] == "REDACTED"
