"""Translate Forge ``Model*`` DTOs <-> provider SDK request/response shapes.

Pure, SDK-free mapping (operates on plain objects/dicts), so it is unit-testable
against recorded provider-shaped fixtures with neither ``anthropic`` nor
``openai`` installed. Two concerns per provider:

* build the provider request (messages, system, tool schemas) from a
  ``ModelRequest``;
* fold a provider response back into a ``ModelResponse`` (text -> ``content``,
  a ``tool_use`` / ``tool_calls`` block -> ``ModelToolCall``, usage ->
  ``TokenUsage``, and a safety stop -> ``stop_reason="refusal"``).
"""

from __future__ import annotations

import json
from typing import Any

from forge_agent.providers import effort as effort_map
from forge_contracts import (
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    TokenUsage,
)

__all__ = [
    "anthropic_cache_read_tokens",
    "anthropic_stream_kwargs",
    "build_local_cli_prompt",
    "from_anthropic_message",
    "from_openai_completion",
    "local_cli_output_schema",
    "openai_create_kwargs",
    "parse_local_cli_response",
    "to_anthropic_tools",
    "to_openai_tools",
]

#: Schema advertised for a tool that declares no ``input_schema``.
_EMPTY_INPUT_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


def _tool_input_schema(tool: dict[str, Any]) -> dict[str, Any]:
    schema = tool.get("input_schema")
    if isinstance(schema, dict) and schema:
        return schema
    return dict(_EMPTY_INPUT_SCHEMA)


# --------------------------------------------------------------------------- #
# Anthropic                                                                    #
# --------------------------------------------------------------------------- #


def to_anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map Forge tool schemas to Anthropic ``tools=[{name, description, input_schema}]``."""
    return [
        {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "input_schema": _tool_input_schema(tool),
        }
        for tool in tools
    ]


def _anthropic_messages(request: ModelRequest) -> list[dict[str, Any]]:
    return [{"role": m.role, "content": m.content} for m in request.messages]


def anthropic_stream_kwargs(
    request: ModelRequest,
    *,
    model: str,
    effort: str,
    max_tokens: int,
    prompt_cache: bool,
) -> dict[str, Any]:
    """Build the kwargs for ``client.messages.stream(...)``.

    Uses adaptive thinking + ``output_config.effort`` per the claude-api guidance
    (no ``budget_tokens``/``temperature`` — those 400 on Opus 4.8). The ao-config
    effort level is mapped to a valid Claude ``effort`` via
    :func:`forge_agent.providers.effort.anthropic_effort` (ao-effort). When
    ``prompt_cache`` is on, the stable system prompt carries a ``cache_control``
    breakpoint so re-sent turns read it from cache.
    """
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": _anthropic_messages(request),
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort_map.anthropic_effort(effort)},
    }
    system = request.system
    if system:
        if prompt_cache:
            kwargs["system"] = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        else:
            kwargs["system"] = system
    if request.tools:
        kwargs["tools"] = to_anthropic_tools(request.tools)
    if request.stop:
        kwargs["stop_sequences"] = list(request.stop)
    return kwargs


def _anthropic_usage(msg: Any) -> TokenUsage | None:
    usage = getattr(msg, "usage", None)
    if usage is None:
        return None
    return TokenUsage(
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
    )


def anthropic_cache_read_tokens(msg: Any) -> int:
    """Prompt-cache read tokens (for cost only; not on the frozen DTO)."""
    usage = getattr(msg, "usage", None)
    if usage is None:
        return 0
    return int(getattr(usage, "cache_read_input_tokens", 0) or 0)


def from_anthropic_message(msg: Any) -> ModelResponse:
    """Fold an Anthropic message into a ``ModelResponse``.

    Guards the refusal stop **before** reading ``content`` (claude-api pitfall):
    a refused message may carry no usable content, and its category (not the
    refusal text) is surfaced via ``stop_reason``.
    """
    stop_reason = getattr(msg, "stop_reason", None)
    usage = _anthropic_usage(msg)
    model = getattr(msg, "model", None)

    if stop_reason == "refusal":
        category = _refusal_category(msg)
        return ModelResponse(
            content="",
            model=model,
            stop_reason=f"refusal:{category}" if category else "refusal",
            usage=usage,
        )

    text_parts: list[str] = []
    tool_calls: list[ModelToolCall] = []
    for block in getattr(msg, "content", None) or []:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            text_parts.append(getattr(block, "text", "") or "")
        elif block_type == "tool_use":
            tool_calls.append(
                ModelToolCall(
                    id=getattr(block, "id", None),
                    name=getattr(block, "name", ""),
                    arguments=dict(getattr(block, "input", None) or {}),
                )
            )
    return ModelResponse(
        content="".join(text_parts),
        model=model,
        stop_reason=stop_reason,
        tool_calls=tool_calls,
        usage=usage,
    )


def _refusal_category(msg: Any) -> str | None:
    details = getattr(msg, "stop_details", None)
    if details is None:
        return None
    category = getattr(details, "category", None)
    return str(category) if category else None


# --------------------------------------------------------------------------- #
# OpenAI                                                                       #
# --------------------------------------------------------------------------- #


def to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map Forge tool schemas to OpenAI ``tools=[{type:function, function:{...}}]``."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": _tool_input_schema(tool),
            },
        }
        for tool in tools
    ]


def _openai_messages(request: ModelRequest) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if request.system:
        messages.append({"role": "system", "content": request.system})
    messages.extend({"role": m.role, "content": m.content} for m in request.messages)
    return messages


def openai_create_kwargs(
    request: ModelRequest, *, model: str, max_tokens: int, effort: Any = "high"
) -> dict[str, Any]:
    """Build the kwargs for ``client.chat.completions.stream(...)``.

    The ao-config effort level is mapped to OpenAI's ``reasoning_effort`` via
    :func:`forge_agent.providers.effort.openai_reasoning_effort` (ao-effort), but
    only for reasoning models (``o*`` / ``gpt-5``) — non-reasoning models reject
    the parameter with a 400.
    """
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": _openai_messages(request),
        "max_tokens": max_tokens,
    }
    if effort_map.openai_supports_reasoning_effort(model):
        kwargs["reasoning_effort"] = effort_map.openai_reasoning_effort(effort)
    if request.tools:
        kwargs["tools"] = to_openai_tools(request.tools)
    if request.stop:
        kwargs["stop"] = list(request.stop)
    return kwargs


def _openai_usage(completion: Any) -> TokenUsage | None:
    usage = getattr(completion, "usage", None)
    if usage is None:
        return None
    return TokenUsage(
        input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
    )


def from_openai_completion(completion: Any) -> ModelResponse:
    """Fold an OpenAI chat completion into a ``ModelResponse``.

    A ``content_filter`` finish maps to ``stop_reason="refusal"``; ``tool_calls``
    become ``ModelToolCall`` (JSON string arguments are parsed).
    """
    usage = _openai_usage(completion)
    model = getattr(completion, "model", None)
    choices = getattr(completion, "choices", None) or []
    if not choices:
        return ModelResponse(content="", model=model, stop_reason=None, usage=usage)

    choice = choices[0]
    finish_reason = getattr(choice, "finish_reason", None)
    message = getattr(choice, "message", None)

    if finish_reason == "content_filter":
        return ModelResponse(content="", model=model, stop_reason="refusal", usage=usage)

    content = getattr(message, "content", None) or "" if message is not None else ""
    tool_calls: list[ModelToolCall] = []
    raw_tool_calls = getattr(message, "tool_calls", None) or [] if message is not None else []
    for call in raw_tool_calls:
        function = getattr(call, "function", None)
        if function is None:
            continue
        tool_calls.append(
            ModelToolCall(
                id=getattr(call, "id", None),
                name=getattr(function, "name", "") or "",
                arguments=_parse_arguments(getattr(function, "arguments", None)),
            )
        )
    return ModelResponse(
        content=content,
        model=model,
        stop_reason=finish_reason,
        tool_calls=tool_calls,
        usage=usage,
    )


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# --------------------------------------------------------------------------- #
# Local CLI                                                                    #
# --------------------------------------------------------------------------- #

_ENVELOPE_INSTRUCTION = """
Reply with a single JSON object and nothing else:

{"content": "<prose, may be empty>",
 "tool_calls": [{"name": "<tool name>", "arguments": {<json object>}}],
 "stop_reason": "tool_use" | "end_turn"}

Use "tool_calls" only for the tools listed below. Emit an empty list when you
have no tool to call. Do not invoke any tool yourself; only describe the call.
"""

#: Maximum number of candidate JSON objects to scan before giving up.
#: This is the load-bearing complexity bound for _extract_json_object: with early
#: returns for "no } anywhere after start", and this cap, complexity is O(64n) = O(n).
#: A real CLI reply does not contain 64 balanced objects before the envelope.
_MAX_CANDIDATE_ATTEMPTS = 64


def local_cli_output_schema() -> dict[str, Any]:
    """JSON Schema for the envelope, for CLIs that can enforce one."""
    return {
        "type": "object",
        "required": ["content", "tool_calls", "stop_reason"],
        "additionalProperties": False,
        "properties": {
            "content": {"type": "string"},
            "stop_reason": {"type": "string", "enum": ["tool_use", "end_turn"]},
            "tool_calls": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["name", "arguments"],
                    "additionalProperties": False,
                    "properties": {
                        "name": {"type": "string"},
                        "arguments": {"type": "object"},
                    },
                },
            },
            "usage": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "input_tokens": {"type": "integer", "minimum": 0},
                    "output_tokens": {"type": "integer", "minimum": 0},
                },
            },
        },
    }


def build_local_cli_prompt(request: ModelRequest) -> tuple[str, str]:
    """Return ``(system, user)`` for a CLI invocation."""
    parts: list[str] = []
    if request.system:
        parts.append(request.system)
    parts.append(_ENVELOPE_INSTRUCTION.strip())
    if request.tools:
        # default=str: a tool schema is advisory prompt text, not a contract; a
        # non-JSON-serialisable value inside it (e.g. a stray datetime) should
        # degrade to its string form rather than crash prompt building.
        parts.append(
            "Available tools:\n" + json.dumps(request.tools, indent=2, default=str)
        )
    system = "\n\n".join(parts)
    user = "\n\n".join(m.content for m in request.messages if m.content)
    return system, user


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    """Find a JSON object in ``raw`` that looks like an envelope; tolerate surrounding prose.

    Scans for multiple candidate JSON objects, skipping those that fail to parse or
    lack the required envelope fields (content, tool_calls, stop_reason).

    For each candidate, either the scan closes (depth==0) at some }, or runs to EOF
    with depth>0. In either case, if the object is not a valid envelope, we advance
    to the next { and retry. Capped at _MAX_CANDIDATE_ATTEMPTS to prevent adversarial
    input from consuming unbounded CPU.
    """
    start = 0
    attempts = 0
    while attempts < _MAX_CANDIDATE_ATTEMPTS:
        start = raw.find("{", start)
        if start == -1:
            return None

        # Sound early return: if there is no closing brace anywhere after this position,
        # no candidate rooted here or later can possibly close. Stop entirely.
        if raw.find("}", start) == -1:
            return None

        depth = 0
        in_string = False
        escaped = False

        for index in range(start, len(raw)):
            char = raw[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(raw[start : index + 1])
                    except json.JSONDecodeError:
                        break  # This candidate failed; try next
                    if isinstance(parsed, dict):
                        # Check if it has the required envelope fields
                        if all(key in parsed for key in ["content", "tool_calls", "stop_reason"]):
                            return parsed
                        # Not a valid envelope; try next candidate
                    break  # Not a dict; try next candidate

        # Scan closed but invalid, or ran to EOF unclosed: try next candidate
        start += 1
        attempts += 1

    return None


def parse_local_cli_response(raw: str) -> ModelResponse:
    """Fold a CLI envelope into a ``ModelResponse``.

    Unparseable output yields ``stop_reason="parse_error"`` — never an empty
    ``tool_calls`` list, which would be indistinguishable from the model
    deliberately choosing to do nothing.

    Validates that required fields (content, tool_calls, stop_reason) are present
    and of the correct types. Rejects envelopes with invalid tool_calls entries.
    An optional ``usage`` that violates the schema (not an object, or an
    ``input_tokens``/``output_tokens`` that isn't a non-negative integer) is
    likewise ``parse_error`` rather than silently coerced or dropped: this
    field comes straight from CLI-controlled output, the same untrusted
    surface this parser otherwise polices strictly.
    """
    payload = _extract_json_object(raw)
    if payload is None:
        return ModelResponse(content="", stop_reason="parse_error")

    # Validate required top-level fields are present and correct type
    if not all(key in payload for key in ["content", "tool_calls", "stop_reason"]):
        return ModelResponse(content="", stop_reason="parse_error")

    if not isinstance(payload.get("content"), str):
        return ModelResponse(content="", stop_reason="parse_error")

    if not isinstance(payload.get("tool_calls"), list):
        return ModelResponse(content="", stop_reason="parse_error")

    if not isinstance(payload.get("stop_reason"), str):
        return ModelResponse(content="", stop_reason="parse_error")

    calls: list[ModelToolCall] = []
    for entry in payload.get("tool_calls") or []:
        if not isinstance(entry, dict) or not entry.get("name"):
            return ModelResponse(content="", stop_reason="parse_error")
        # arguments is required and must be a dict
        if "arguments" not in entry or not isinstance(entry.get("arguments"), dict):
            return ModelResponse(content="", stop_reason="parse_error")
        calls.append(ModelToolCall(name=str(entry["name"]), arguments=entry["arguments"]))

    usage_raw = payload.get("usage")
    if usage_raw is not None:
        if not isinstance(usage_raw, dict):
            return ModelResponse(content="", stop_reason="parse_error")
        for token_field in ("input_tokens", "output_tokens"):
            if token_field not in usage_raw:
                continue
            value = usage_raw[token_field]
            # bool is a subclass of int in Python; JSON Schema's "integer"
            # does not admit true/false, so exclude it explicitly.
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return ModelResponse(content="", stop_reason="parse_error")

    usage = usage_raw or {}
    return ModelResponse(
        content=payload["content"],
        tool_calls=calls,
        stop_reason=payload["stop_reason"],
        usage=TokenUsage(
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
        )
        if usage
        else None,
    )
