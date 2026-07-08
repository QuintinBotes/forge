"""Factory + error types for the provider-agnostic model client (HARD-02).

``build_model_client`` selects the adapter from a ``ModelClientConfig`` and wires
the injected ``redactor`` (the worker/API pass ``forge_api`` redaction). The SDK
client can be injected (``client=``) for hermetic tests; otherwise the adapter
builds the real SDK client lazily and raises ``ModelClientUnavailable`` when the
optional provider SDK is not installed.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from forge_agent.providers.config import ModelClientConfig, ProviderName
from forge_contracts import ModelClient

__all__ = ["ModelClientError", "ModelClientUnavailable", "build_model_client"]


class ModelClientError(RuntimeError):
    """A provider call failed; the message is already redactor-scrubbed."""


class ModelClientUnavailable(ModelClientError):
    """The provider SDK is not installed / no usable client can be built.

    Raised on the integration lane when ``forge-agent[providers]`` is absent, so
    the live tests can ``skip`` cleanly rather than falling back to a fake.
    """


def _identity(value: str) -> str:
    return value


def build_model_client(
    config: ModelClientConfig,
    *,
    redactor: Callable[[str], str] = _identity,
    client: Any | None = None,
) -> ModelClient:
    """Build the adapter for ``config.provider`` (unknown -> ``ModelClientError``)."""
    provider = config.provider
    if provider is ProviderName.anthropic:
        from forge_agent.providers.anthropic_client import AnthropicModelClient

        return AnthropicModelClient(
            model=config.model,
            api_key=config.api_key,
            effort=config.effort,
            max_tokens=config.max_tokens,
            timeout_s=config.timeout_s,
            max_retries=config.max_retries,
            base_url=config.base_url,
            prompt_cache=config.prompt_cache,
            redactor=redactor,
            client=client,
        )
    if provider is ProviderName.openai:
        from forge_agent.providers.openai_client import OpenAIModelClient

        return OpenAIModelClient(
            model=config.model,
            api_key=config.api_key,
            effort=config.effort,
            max_tokens=config.max_tokens,
            timeout_s=config.timeout_s,
            max_retries=config.max_retries,
            base_url=config.base_url,
            redactor=redactor,
            client=client,
        )
    raise ModelClientError(f"unknown model provider: {provider!r}")
