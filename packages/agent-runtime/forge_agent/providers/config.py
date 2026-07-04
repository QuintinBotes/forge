"""Provider selection + BYOK config for the model client (HARD-02).

``ModelClientConfig`` is the immutable, DI-friendly bundle the factory turns into
an adapter. It carries the resolved ``api_key`` but never echoes it: the field is
excluded from ``repr`` so a config in a log/trace/exception never leaks the key.

``from_env`` reads the documented ``FORGE_MODEL_*`` variables (plus the
provider-native ``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY``). ``FORGE_MODEL_PROVIDER``
is the master switch — absent, ``from_env`` returns ``None`` so the worker keeps
the offline scripted client and the hermetic suite never touches a real provider.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = ["ModelClientConfig", "ProviderName"]


class ProviderName(StrEnum):
    """The BYOK model providers HARD-02 ships adapters for."""

    anthropic = "anthropic"
    openai = "openai"


#: Provider default model. Anthropic has a reference default (claude-api); OpenAI
#: has none — the operator must choose ``FORGE_MODEL_NAME`` (``from_env`` returns
#: ``None`` otherwise).
_DEFAULT_MODEL: dict[ProviderName, str | None] = {
    ProviderName.anthropic: "claude-opus-4-8",
    ProviderName.openai: None,
}

#: Env var holding each provider's BYOK key.
_KEY_ENV: dict[ProviderName, str] = {
    ProviderName.anthropic: "ANTHROPIC_API_KEY",
    ProviderName.openai: "OPENAI_API_KEY",
}

_TRUE = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class ModelClientConfig:
    """Immutable provider config; ``api_key`` is never included in ``repr``."""

    provider: ProviderName
    model: str
    api_key: str = field(repr=False)
    effort: str = "high"
    max_tokens: int = 16000
    timeout_s: float = 600.0
    max_retries: int = 2
    base_url: str | None = None
    prompt_cache: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ModelClientConfig | None:
        """Build a config from ``FORGE_MODEL_*`` env vars, or ``None`` if unset.

        Returns ``None`` (degrade to the offline path) when ``FORGE_MODEL_PROVIDER``
        is unset/unknown, the provider's BYOK key is absent, or no model can be
        resolved. Never raises on a missing/typo'd provider.
        """
        source = os.environ if env is None else env
        raw_provider = (source.get("FORGE_MODEL_PROVIDER") or "").strip().lower()
        if not raw_provider:
            return None
        try:
            provider = ProviderName(raw_provider)
        except ValueError:
            return None

        api_key = (
            source.get(_KEY_ENV[provider]) or source.get("FORGE_MODEL_API_KEY") or ""
        ).strip()
        if not api_key:
            return None

        model = (source.get("FORGE_MODEL_NAME") or "").strip() or _DEFAULT_MODEL[provider]
        if not model:
            return None

        return cls(
            provider=provider,
            model=model,
            api_key=api_key,
            effort=(source.get("FORGE_MODEL_EFFORT") or "high").strip() or "high",
            max_tokens=_int(source.get("FORGE_MODEL_MAX_TOKENS"), 16000),
            timeout_s=_float(source.get("FORGE_MODEL_TIMEOUT_S"), 600.0),
            max_retries=_int(source.get("FORGE_MODEL_MAX_RETRIES"), 2),
            base_url=(source.get("FORGE_MODEL_BASE_URL") or "").strip() or None,
            prompt_cache=_bool(source.get("FORGE_MODEL_PROMPT_CACHE"), default=True),
        )


def _int(value: str | None, default: int) -> int:
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _float(value: str | None, default: float) -> float:
    if value is None or not value.strip():
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _bool(value: str | None, *, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in _TRUE
