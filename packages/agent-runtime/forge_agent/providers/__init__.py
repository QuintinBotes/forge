"""Provider-agnostic BYOK model clients (HARD-02).

Public surface for the model-client seam that satisfies the frozen
``forge_contracts.ModelClient`` Protocol with two real adapters (Anthropic +
OpenAI) behind each provider's official SDK. The SDKs are an optional extra
(``forge-agent[providers]``) imported lazily *inside* the adapters, so the
hermetic default suite needs neither installed.
"""

from __future__ import annotations

from forge_agent.providers.anthropic_client import AnthropicModelClient
from forge_agent.providers.base import (
    ModelClientError,
    ModelClientUnavailable,
    build_model_client,
)
from forge_agent.providers.config import ModelClientConfig, ProviderName
from forge_agent.providers.openai_client import OpenAIModelClient
from forge_agent.providers.pricing import MODEL_PRICING, cost_usd
from forge_agent.providers.usage import UsageAccumulator

__all__ = [
    "MODEL_PRICING",
    "AnthropicModelClient",
    "ModelClientConfig",
    "ModelClientError",
    "ModelClientUnavailable",
    "OpenAIModelClient",
    "ProviderName",
    "UsageAccumulator",
    "build_model_client",
    "cost_usd",
]
