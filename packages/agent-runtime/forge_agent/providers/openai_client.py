"""OpenAI BYOK adapter (HARD-02).

Implements the frozen ``forge_contracts.ModelClient`` over the official
``openai`` SDK. ``complete`` runs over the streaming helper
(``chat.completions.stream(...)`` + ``get_final_completion()``) so large
``max_tokens`` never trips an HTTP timeout. Same secret hygiene as the Anthropic
adapter: metadata-only logging, and every SDK exception passes through the
injected ``redactor`` before it is re-raised as ``ModelClientError``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from typing import Any

from forge_agent.providers import translate
from forge_agent.providers.base import ModelClientError, ModelClientUnavailable
from forge_contracts import ModelRequest, ModelResponse, ModelStreamEvent

__all__ = ["OpenAIModelClient"]

_logger = logging.getLogger("forge_agent.providers.openai")


def _identity(value: str) -> str:
    return value


class OpenAIModelClient:
    """A ``ModelClient`` backed by the official ``openai`` SDK."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        effort: str = "high",
        max_tokens: int = 16000,
        timeout_s: float = 600.0,
        max_retries: int = 2,
        base_url: str | None = None,
        redactor: Callable[[str], str] = _identity,
        client: Any | None = None,
    ) -> None:
        self._model = model
        self._effort = effort
        self._max_tokens = max_tokens
        self._redactor = redactor
        self._client = (
            client
            if client is not None
            else _build_sdk_client(
                api_key=api_key,
                timeout_s=timeout_s,
                max_retries=max_retries,
                base_url=base_url,
            )
        )

    def complete(self, request: ModelRequest) -> ModelResponse:
        kwargs = translate.openai_create_kwargs(
            request, model=self._model, max_tokens=self._max_tokens, effort=self._effort
        )
        try:
            with self._client.chat.completions.stream(**kwargs) as stream:
                completion = stream.get_final_completion()
        except Exception as exc:
            raise self._error(exc) from exc
        self._log(completion)
        return translate.from_openai_completion(completion)

    def stream(self, request: ModelRequest) -> Iterator[ModelStreamEvent]:
        kwargs = translate.openai_create_kwargs(
            request, model=self._model, max_tokens=self._max_tokens, effort=self._effort
        )
        try:
            with self._client.chat.completions.stream(**kwargs) as stream:
                for event in stream:
                    text = _event_text(event)
                    if text:
                        yield ModelStreamEvent(type="text", text=text, delta=text)
        except Exception as exc:
            raise self._error(exc) from exc

    # ------------------------------------------------------------------ #
    def _error(self, exc: Exception) -> ModelClientError:
        return ModelClientError(self._redactor(str(exc)))

    def _log(self, completion: Any) -> None:
        usage = getattr(completion, "usage", None)
        _logger.debug(
            "openai.complete model=%s in=%s out=%s",
            self._model,
            getattr(usage, "prompt_tokens", None),
            getattr(usage, "completion_tokens", None),
        )


def _event_text(event: Any) -> str | None:
    """Pull incremental text from an OpenAI stream event, if any."""
    delta = getattr(event, "delta", None)
    if isinstance(delta, str) and delta:
        return delta
    return None


def _build_sdk_client(
    *, api_key: str, timeout_s: float, max_retries: int, base_url: str | None
) -> Any:
    try:
        import openai
    except ImportError as exc:  # pragma: no cover - exercised only w/o the extra
        raise ModelClientUnavailable(
            "the 'openai' SDK is not installed; install forge-agent[providers] "
            "to use the live OpenAI model client"
        ) from exc
    kwargs: dict[str, Any] = {
        "api_key": api_key,
        "timeout": timeout_s,
        "max_retries": max_retries,
    }
    if base_url:
        kwargs["base_url"] = base_url
    return openai.OpenAI(**kwargs)
