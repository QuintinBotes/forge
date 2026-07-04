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

from forge_contracts import (
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    TokenUsage,
)

__all__ = [
    "anthropic_cache_read_tokens",
    "anthropic_stream_kwargs",
    "from_anthropic_message",
    "from_openai_completion",
    "openai_create_kwargs",
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
    (no ``budget_tokens``/``temperature`` — those 400 on Opus 4.8). When
    ``prompt_cache`` is on, the stable system prompt carries a ``cache_control``
    breakpoint so re-sent turns read it from cache.
    """
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": _anthropic_messages(request),
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort},
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


def openai_create_kwargs(request: ModelRequest, *, model: str, max_tokens: int) -> dict[str, Any]:
    """Build the kwargs for ``client.chat.completions.stream(...)``."""
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": _openai_messages(request),
        "max_tokens": max_tokens,
    }
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
