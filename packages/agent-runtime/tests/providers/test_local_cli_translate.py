import pytest

from forge_agent.providers.translate import (
    build_local_cli_prompt,
    local_cli_output_schema,
    parse_local_cli_response,
)
from forge_contracts import ModelMessage, ModelRequest


def _request() -> ModelRequest:
    return ModelRequest(
        model="claude",
        system="You are terse.",
        messages=[ModelMessage(role="user", content="Read app/main.py")],
        tools=[{"name": "read_file", "description": "Read a file",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}}],
    )


def test_prompt_carries_tool_schemas_and_envelope_instruction():
    system, user = build_local_cli_prompt(_request())
    assert "You are terse." in system
    assert "read_file" in system
    assert "tool_calls" in system
    assert "Read app/main.py" in user


def test_parses_tool_calls():
    raw = '{"content": "reading", "tool_calls": [{"name": "read_file", "arguments": {"path": "a.py"}}], "stop_reason": "tool_use"}'
    response = parse_local_cli_response(raw)
    assert response.content == "reading"
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "read_file"
    assert response.tool_calls[0].arguments == {"path": "a.py"}
    assert response.stop_reason == "tool_use"


def test_parses_envelope_wrapped_in_prose():
    """CLIs sometimes surround JSON with commentary; extract the object."""
    raw = 'Sure!\n```json\n{"content": "hi", "tool_calls": [], "stop_reason": "end_turn"}\n```\nDone.'
    response = parse_local_cli_response(raw)
    assert response.content == "hi"
    assert response.stop_reason == "end_turn"


@pytest.mark.parametrize("raw", ["", "not json at all", "{unclosed", "[]"])
def test_unparseable_yields_parse_error_not_empty_tool_calls(raw):
    response = parse_local_cli_response(raw)
    assert response.stop_reason == "parse_error"
    assert response.tool_calls == []


def test_output_schema_is_a_json_schema_object():
    schema = local_cli_output_schema()
    assert schema["type"] == "object"
    assert "tool_calls" in schema["properties"]


def test_schema_permits_optional_usage():
    schema = local_cli_output_schema()
    assert "usage" in schema["properties"]
    assert "usage" not in schema["required"]


def test_usage_is_parsed_when_present():
    raw = (
        '{"content": "hi", "tool_calls": [], "stop_reason": "end_turn",'
        ' "usage": {"input_tokens": 11, "output_tokens": 22}}'
    )
    response = parse_local_cli_response(raw)
    assert response.usage is not None
    assert response.usage.input_tokens == 11
    assert response.usage.output_tokens == 22
