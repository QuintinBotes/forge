"""HARD-02 AC3: ``ModelRequest`` round-trips through each adapter's translator."""

from __future__ import annotations

from _provider_fakes import anthropic_message, openai_completion

from forge_agent.providers import translate
from forge_contracts import ModelMessage, ModelRequest

_TOOLS = [
    {
        "name": "write_file",
        "description": "Write a repo file",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    {"name": "list_dir", "description": "List a directory"},  # no input_schema
]

_REQUEST = ModelRequest(
    model="claude-opus-4-8",
    system="You are Forge.",
    messages=[
        ModelMessage(role="user", content="do the thing"),
        ModelMessage(role="assistant", content="on it"),
        ModelMessage(role="user", content="Result of list_dir: a.py"),
    ],
    tools=_TOOLS,
)


def test_anthropic_request_maps_system_messages_and_tools() -> None:
    kwargs = translate.anthropic_stream_kwargs(
        _REQUEST, model="claude-opus-4-8", effort="high", max_tokens=16000, prompt_cache=True
    )
    assert kwargs["model"] == "claude-opus-4-8"
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["output_config"] == {"effort": "high"}
    # No sampling params (would 400 on Opus 4.8).
    assert "temperature" not in kwargs and "top_p" not in kwargs
    # System prompt carries a cache_control breakpoint when prompt_cache is on.
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert len(kwargs["messages"]) == 3
    tools = kwargs["tools"]
    assert tools[0]["input_schema"]["required"] == ["path", "content"]
    # A tool with no declared input_schema gets a valid empty-object schema.
    assert tools[1]["input_schema"] == {"type": "object", "properties": {}}


def test_anthropic_tool_use_maps_to_single_model_tool_call() -> None:
    msg = anthropic_message(
        tool_uses=[("toolu_1", "write_file", {"path": "x.py", "content": "y"})],
        stop_reason="tool_use",
        input_tokens=11,
        output_tokens=4,
    )
    response = translate.from_anthropic_message(msg)
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert (call.id, call.name, call.arguments) == (
        "toolu_1",
        "write_file",
        {"path": "x.py", "content": "y"},
    )
    assert response.usage is not None and response.usage.input_tokens == 11


def test_anthropic_text_only_maps_to_content() -> None:
    response = translate.from_anthropic_message(anthropic_message(text="all done"))
    assert response.content == "all done"
    assert response.tool_calls == []


def test_openai_request_prepends_system_and_maps_tools() -> None:
    kwargs = translate.openai_create_kwargs(_REQUEST, model="gpt-4o", max_tokens=8000)
    assert kwargs["messages"][0] == {"role": "system", "content": "You are Forge."}
    assert len(kwargs["messages"]) == 4
    assert kwargs["tools"][0]["type"] == "function"
    assert kwargs["tools"][0]["function"]["name"] == "write_file"


def test_openai_tool_call_json_arguments_are_parsed() -> None:
    completion = openai_completion(
        tool_calls=[("call_1", "write_file", '{"path": "x.py", "content": "y"}')],
        finish_reason="tool_calls",
        prompt_tokens=9,
        completion_tokens=3,
    )
    response = translate.from_openai_completion(completion)
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert call.name == "write_file"
    assert call.arguments == {"path": "x.py", "content": "y"}
    assert response.usage is not None and response.usage.output_tokens == 3


def test_openai_text_only_maps_to_content() -> None:
    response = translate.from_openai_completion(openai_completion(content="hello there"))
    assert response.content == "hello there"
    assert response.tool_calls == []
