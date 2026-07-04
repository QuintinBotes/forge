"""Fake provider SDK clients for the hermetic HARD-02 adapter tests.

These stand in for ``anthropic.Anthropic`` / ``openai.OpenAI`` via the adapters'
injectable ``client=`` seam, so the default suite exercises ``complete`` /
``stream`` with neither SDK installed and no network. The response objects mirror
the *shape* the translators read (duck-typed attributes only) — no secrets, no
real ids.
"""

from __future__ import annotations

from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any

__all__ = [
    "FakeAnthropicSDK",
    "FakeOpenAISDK",
    "anthropic_message",
    "openai_completion",
]


# --------------------------------------------------------------------------- #
# Anthropic                                                                    #
# --------------------------------------------------------------------------- #


def anthropic_message(
    *,
    text: str = "",
    tool_uses: Iterable[tuple[str, str, dict[str, Any]]] = (),
    stop_reason: str = "end_turn",
    model: str = "claude-opus-4-8",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    refusal_category: str | None = None,
    request_id: str = "req_fake_0001",
) -> SimpleNamespace:
    """Build a fake Anthropic final message with duck-typed blocks/usage."""
    content: list[SimpleNamespace] = []
    if text:
        content.append(SimpleNamespace(type="text", text=text))
    for tool_id, name, args in tool_uses:
        content.append(SimpleNamespace(type="tool_use", id=tool_id, name=name, input=args))
    usage = SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
    )
    stop_details = (
        SimpleNamespace(type="refusal", category=refusal_category)
        if refusal_category is not None
        else None
    )
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        model=model,
        usage=usage,
        stop_details=stop_details,
        _request_id=request_id,
    )


class _AnthropicStreamCtx:
    def __init__(self, message: Any, text_chunks: Iterable[str]) -> None:
        self._message = message
        self._chunks = list(text_chunks)

    def __enter__(self) -> _AnthropicStreamCtx:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def get_final_message(self) -> Any:
        return self._message

    @property
    def text_stream(self):
        return iter(self._chunks)


class _AnthropicMessages:
    def __init__(self, sdk: FakeAnthropicSDK) -> None:
        self._sdk = sdk

    def stream(self, **kwargs: Any) -> _AnthropicStreamCtx:
        self._sdk.captured_kwargs.append(kwargs)
        if self._sdk.error is not None:
            raise self._sdk.error
        return _AnthropicStreamCtx(self._sdk.message, self._sdk.text_chunks)


class FakeAnthropicSDK:
    """Stand-in for ``anthropic.Anthropic`` exposing ``messages.stream(...)``."""

    def __init__(
        self,
        *,
        message: Any = None,
        text_chunks: Iterable[str] = (),
        error: Exception | None = None,
    ) -> None:
        self.message = message if message is not None else anthropic_message(text="ok")
        self.text_chunks = list(text_chunks)
        self.error = error
        self.captured_kwargs: list[dict[str, Any]] = []
        self.messages = _AnthropicMessages(self)


# --------------------------------------------------------------------------- #
# OpenAI                                                                       #
# --------------------------------------------------------------------------- #


def openai_completion(
    *,
    content: str = "",
    tool_calls: Iterable[tuple[str, str, str]] = (),
    finish_reason: str = "stop",
    model: str = "gpt-4o",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> SimpleNamespace:
    """Build a fake OpenAI chat completion (``choices[0].message`` + usage)."""
    calls = [
        SimpleNamespace(
            id=call_id,
            function=SimpleNamespace(name=name, arguments=arguments),
        )
        for call_id, name, arguments in tool_calls
    ]
    message = SimpleNamespace(content=content or None, tool_calls=calls or None)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    usage = SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    return SimpleNamespace(choices=[choice], usage=usage, model=model)


class _OpenAIStreamCtx:
    def __init__(self, completion: Any, events: Iterable[Any]) -> None:
        self._completion = completion
        self._events = list(events)

    def __enter__(self) -> _OpenAIStreamCtx:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def __iter__(self):
        return iter(self._events)

    def get_final_completion(self) -> Any:
        return self._completion


class _OpenAICompletions:
    def __init__(self, sdk: FakeOpenAISDK) -> None:
        self._sdk = sdk

    def stream(self, **kwargs: Any) -> _OpenAIStreamCtx:
        self._sdk.captured_kwargs.append(kwargs)
        if self._sdk.error is not None:
            raise self._sdk.error
        return _OpenAIStreamCtx(self._sdk.completion, self._sdk.events)


class FakeOpenAISDK:
    """Stand-in for ``openai.OpenAI`` exposing ``chat.completions.stream(...)``."""

    def __init__(
        self,
        *,
        completion: Any = None,
        events: Iterable[Any] = (),
        error: Exception | None = None,
    ) -> None:
        self.completion = completion if completion is not None else openai_completion(content="ok")
        self.events = [SimpleNamespace(delta=chunk) for chunk in events]
        self.error = error
        self.captured_kwargs: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=_OpenAICompletions(self))
