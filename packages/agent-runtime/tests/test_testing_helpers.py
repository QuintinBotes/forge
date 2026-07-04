"""Unit tests for the deterministic test doubles (``forge_agent.testing``)."""

from __future__ import annotations

from forge_agent.testing import ScriptedModelClient, finish_response, tool_response
from forge_contracts import ModelRequest, ModelResponse


def test_finish_response_includes_all_optional_fields() -> None:
    resp = finish_response(
        "out",
        summary="a summary",
        confidence=0.8,
        acceptance_criteria_satisfied=["A1"],
        needs_human=True,
        risks=["danger"],
    )
    assert resp.tool_calls
    args = resp.tool_calls[0].arguments
    assert args["output"] == "out"
    assert args["summary"] == "a summary"
    assert args["confidence"] == 0.8
    assert args["acceptance_criteria_satisfied"] == ["A1"]
    assert args["needs_human"] is True
    assert args["risks"] == ["danger"]


def test_finish_response_omits_unset_optional_fields() -> None:
    args = finish_response("out").tool_calls[0].arguments
    assert args == {"output": "out"}


def test_tool_response_requests_single_call() -> None:
    resp = tool_response("read_file", {"path": "a.py"})
    assert resp.stop_reason == "tool_use"
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "read_file"
    assert resp.tool_calls[0].arguments == {"path": "a.py"}


def test_scripted_client_replays_then_falls_back_to_default() -> None:
    default = finish_response("fallback", confidence=0.5)
    client = ScriptedModelClient([tool_response("t")], default=default)
    first = client.complete(ModelRequest(model="m"))
    assert first.tool_calls[0].name == "t"
    # Script exhausted -> the default response is returned for every later call.
    second = client.complete(ModelRequest(model="m"))
    assert second is default
    assert len(client.requests) == 2


def test_scripted_client_returns_empty_stop_when_exhausted_without_default() -> None:
    client = ScriptedModelClient([])
    resp = client.complete(ModelRequest(model="m"))
    assert isinstance(resp, ModelResponse)
    assert resp.content == ""
    assert resp.stop_reason == "end_turn"


def test_scripted_client_stream_yields_text_event() -> None:
    client = ScriptedModelClient([ModelResponse(content="hello", stop_reason="end_turn")])
    events = list(client.stream(ModelRequest(model="m")))
    assert events
    assert events[0].type == "text"
    assert events[0].text == "hello"
