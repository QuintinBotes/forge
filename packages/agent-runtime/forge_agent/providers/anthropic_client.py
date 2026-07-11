"""Anthropic BYOK adapter (HARD-02, reference provider).

Implements the frozen ``forge_contracts.ModelClient`` over the official
``anthropic`` SDK. ``complete`` runs **over streaming** (``messages.stream(...)``
+ ``get_final_message()``) per the claude-api guidance so large ``max_tokens``
never trips the SDK's HTTP-timeout guard.

Secret hygiene: the adapter never logs request/response bodies (only metadata —
provider, model, token counts, request id), and passes any SDK exception through
the injected ``redactor`` before re-raising it as ``ModelClientError``, so a
provider error message that echoes a key/token is scrubbed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from typing import Any

from forge_agent.providers import translate
from forge_agent.providers.base import ModelClientError, ModelClientUnavailable
from forge_contracts import ModelRequest, ModelResponse, ModelStreamEvent

__all__ = ["AnthropicModelClient"]

_logger = logging.getLogger("forge_agent.providers.anthropic")


def _identity(value: str) -> str:
    return value


class AnthropicModelClient:
    """A ``ModelClient`` backed by the official ``anthropic`` SDK."""

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
        prompt_cache: bool = True,
        redactor: Callable[[str], str] = _identity,
        client: Any | None = None,
    ) -> None:
        self._model = model
        self._effort = effort
        self._max_tokens = max_tokens
        self._prompt_cache = prompt_cache
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
        model = self._resolve_model(request)
        kwargs = translate.anthropic_stream_kwargs(
            request,
            model=model,
            effort=self._effort,
            max_tokens=self._max_tokens,
            prompt_cache=self._prompt_cache,
        )
        try:
            with self._client.messages.stream(**kwargs) as stream:
                message = stream.get_final_message()
        except Exception as exc:  # redact + normalise every provider failure
            raise self._error(exc) from exc
        self._log(message, model)
        return translate.from_anthropic_message(message)

    def stream(self, request: ModelRequest) -> Iterator[ModelStreamEvent]:
        kwargs = translate.anthropic_stream_kwargs(
            request,
            model=self._resolve_model(request),
            effort=self._effort,
            max_tokens=self._max_tokens,
            prompt_cache=self._prompt_cache,
        )
        try:
            with self._client.messages.stream(**kwargs) as stream:
                for text in stream.text_stream:
                    yield ModelStreamEvent(type="text", text=text, delta=text)
        except Exception as exc:
            raise self._error(exc) from exc

    # ------------------------------------------------------------------ #
    def _resolve_model(self, request: ModelRequest) -> str:
        """Honor a per-request model (e.g. an Adaptive Orchestration per-role
        model routed by :class:`~forge_agent.providers.router.ModelRouter`),
        falling back to the constructor-bound default when ``request.model`` is
        unset. Keeps single-agent callers that leave ``request.model`` empty
        pinned to their configured model."""
        return request.model or self._model

    def _error(self, exc: Exception) -> ModelClientError:
        return ModelClientError(self._redactor(str(exc)))

    def _log(self, message: Any, model: str) -> None:
        usage = getattr(message, "usage", None)
        _logger.debug(
            "anthropic.complete model=%s stop=%s in=%s out=%s cache_read=%s req_id=%s",
            model,
            getattr(message, "stop_reason", None),
            getattr(usage, "input_tokens", None),
            getattr(usage, "output_tokens", None),
            translate.anthropic_cache_read_tokens(message),
            getattr(message, "_request_id", None),
        )


def _build_sdk_client(
    *, api_key: str, timeout_s: float, max_retries: int, base_url: str | None
) -> Any:
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - exercised only w/o the extra
        raise ModelClientUnavailable(
            "the 'anthropic' SDK is not installed; install forge-agent[providers] "
            "to use the live Anthropic model client"
        ) from exc
    kwargs: dict[str, Any] = {
        "api_key": api_key,
        "timeout": timeout_s,
        "max_retries": max_retries,
    }
    if base_url:
        kwargs["base_url"] = base_url
    return anthropic.Anthropic(**kwargs)
