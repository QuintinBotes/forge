import datetime
import time

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
        tools=[
            {
                "name": "read_file",
                "description": "Read a file",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
            }
        ],
    )


def test_prompt_carries_tool_schemas_and_envelope_instruction():
    system, user = build_local_cli_prompt(_request())
    assert "You are terse." in system
    assert "read_file" in system
    assert "tool_calls" in system
    assert "Read app/main.py" in user


def test_non_serialisable_tool_schema_value_degrades_instead_of_crashing():
    """IMPORTANT: a tool schema is internal (not attacker-controlled) data,
    but json.dumps(request.tools) must not crash prompt building on a value
    it can't natively serialise (e.g. a stray datetime); it should degrade
    to that value's string form instead.
    """
    request = ModelRequest(
        model="claude",
        messages=[ModelMessage(role="user", content="hi")],
        tools=[{"name": "t", "when": datetime.datetime(2026, 1, 1)}],
    )
    system, _user = build_local_cli_prompt(request)
    assert "2026-01-01" in system


def test_parses_tool_calls():
    raw = (
        '{"content": "reading", "tool_calls": [{"name": "read_file", '
        '"arguments": {"path": "a.py"}}], "stop_reason": "tool_use"}'
    )
    response = parse_local_cli_response(raw)
    assert response.content == "reading"
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "read_file"
    assert response.tool_calls[0].arguments == {"path": "a.py"}
    assert response.stop_reason == "tool_use"


def test_parses_envelope_wrapped_in_prose():
    """CLIs sometimes surround JSON with commentary; extract the object."""
    raw = (
        'Sure!\n```json\n{"content": "hi", "tool_calls": [], "stop_reason": "end_turn"}\n```\nDone.'
    )
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


def test_non_integer_usage_token_yields_parse_error():
    """CRITICAL: usage is CLI-controlled output; a non-integer input_tokens
    must not crash the parser (previously ``int("abc")`` raised ValueError).
    """
    raw = (
        '{"content": "hi", "tool_calls": [], "stop_reason": "end_turn",'
        ' "usage": {"input_tokens": "abc"}}'
    )
    response = parse_local_cli_response(raw)
    assert response.stop_reason == "parse_error"
    assert response.tool_calls == []


def test_usage_not_an_object_yields_parse_error():
    raw = '{"content": "hi", "tool_calls": [], "stop_reason": "end_turn", "usage": "bad"}'
    response = parse_local_cli_response(raw)
    assert response.stop_reason == "parse_error"


def test_negative_usage_token_yields_parse_error():
    raw = (
        '{"content": "hi", "tool_calls": [], "stop_reason": "end_turn",'
        ' "usage": {"input_tokens": -1}}'
    )
    response = parse_local_cli_response(raw)
    assert response.stop_reason == "parse_error"


def test_boolean_usage_token_yields_parse_error():
    """bool is a subclass of int in Python; JSON Schema's "integer" does not
    admit true/false, so it must be rejected explicitly."""
    raw = (
        '{"content": "hi", "tool_calls": [], "stop_reason": "end_turn",'
        ' "usage": {"input_tokens": true}}'
    )
    response = parse_local_cli_response(raw)
    assert response.stop_reason == "parse_error"


def test_skips_incidental_json_before_real_envelope():
    """CRITICAL: Don't commit to the first {; scan for a valid envelope."""
    raw = (
        'Note: {"foo": "bar"} is unrelated.\n{"content": "hi", '
        '"tool_calls": [{"name": "read_file", "arguments": {"path": "a.py"}}], '
        '"stop_reason": "tool_use"}'
    )
    response = parse_local_cli_response(raw)
    assert response.content == "hi"
    assert response.stop_reason == "tool_use"
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "read_file"


def test_missing_required_fields_yields_parse_error():
    """IMPORTANT: A valid JSON object without required envelope fields is parse_error."""
    raw = '{"foo": 1}'
    response = parse_local_cli_response(raw)
    assert response.stop_reason == "parse_error"
    assert response.tool_calls == []


def test_missing_arguments_in_tool_call_yields_parse_error():
    """MINOR: A tool call without arguments is parse_error, not defaulted."""
    raw = '{"content": "hi", "tool_calls": [{"name": "read_file"}], "stop_reason": "tool_use"}'
    response = parse_local_cli_response(raw)
    assert response.stop_reason == "parse_error"
    assert response.tool_calls == []


def test_null_arguments_in_tool_call_yields_parse_error():
    """MINOR: A tool call with null arguments is parse_error, not defaulted."""
    raw = (
        '{"content": "hi", "tool_calls": [{"name": "read_file", '
        '"arguments": null}], "stop_reason": "tool_use"}'
    )
    response = parse_local_cli_response(raw)
    assert response.stop_reason == "parse_error"
    assert response.tool_calls == []


def test_deep_unclosed_braces_completes_quickly():
    """Performance regression: unclosed braces should not trigger O(n²) scan."""
    # 10,000 unclosed braces should complete in < 0.5s (linear, not quadratic).
    # Quadratic would take ~2+ seconds based on measured baseline.
    raw = "{" * 10000
    start_time = time.time()
    response = parse_local_cli_response(raw)
    elapsed = time.time() - start_time

    assert response.stop_reason == "parse_error"
    assert response.tool_calls == []
    assert elapsed < 0.5, f"Scanner took {elapsed:.3f}s; quadratic regression detected"


def test_multiple_invalid_objects_before_envelope():
    """Scan should skip multiple balanced-but-invalid JSON objects."""
    # Multiple invalid envelopes before the real one
    raw = (
        '{"invalid": 1} '
        '{"missing": "fields"} '
        '{"content": "hi", "tool_calls": [{"name": "read_file", '
        '"arguments": {"path": "a.py"}}], "stop_reason": "tool_use"}'
    )
    response = parse_local_cli_response(raw)
    assert response.content == "hi"
    assert response.stop_reason == "tool_use"
    assert len(response.tool_calls) == 1


def test_unmatched_leading_braces_do_not_prevent_later_envelope():
    """Unmatched leading braces consume closing braces; later { can still close.

    This was the regression in round 2: early return on EOF-with-depth>0 was unsound.
    Stray leading braces in CLI prose (markdown, code) are plausible.
    """
    raw = (
        '{{{{{ {"content": "hi", "tool_calls": [{"name": "read_file", '
        '"arguments": {"path": "a.py"}}], "stop_reason": "tool_use"}'
    )
    response = parse_local_cli_response(raw)
    assert response.content == "hi"
    assert response.stop_reason == "tool_use"
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "read_file"


def test_no_closing_brace_short_circuits_without_scanning():
    """Regression detector: rule 1 early return is load-bearing.

    Rule 1 checks for any } after start. If none exists, we return immediately
    without scanning. This is essential for perf with adversarial input.

    This n is chosen so the test fails if the find('}') early return is removed;
    do not reduce it. With rule 1: instant. Without rule 1 (cap-only fallback):
    ~2.65s at n=1,000,000. Threshold 1.0s allows ~1000x gap, immune to machine speed.
    """
    # n=1,000,000 unclosed braces: no closing brace anywhere
    raw = "{" * 1_000_000
    start_time = time.time()
    response = parse_local_cli_response(raw)
    elapsed = time.time() - start_time

    assert response.stop_reason == "parse_error"
    assert response.tool_calls == []
    # Rule 1 (find('}', start) == -1) makes this instant; cap-only would take ~2.65s
    assert elapsed < 1.0, f"Scanner took {elapsed:.3f}s; rule 1 early return may be missing"
