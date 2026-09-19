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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from forge_agent.providers.local_cli.detect import CliName, DetectedCli, detect_clis

__all__ = ["ModelClientConfig", "ProviderName"]


class ProviderName(StrEnum):
    """The BYOK model providers HARD-02 ships adapters for."""

    anthropic = "anthropic"
    openai = "openai"
    local_cli = "local_cli"


#: Provider default model. Anthropic has a reference default (claude-api); OpenAI
#: has none — the operator must choose ``FORGE_MODEL_NAME`` (``from_env`` returns
#: ``None`` otherwise). ``local_cli`` is namespaced (``local-cli``/``local-cli:<cli>``)
#: so it can never collide with a real API model's pricing row.
_DEFAULT_MODEL: dict[ProviderName, str | None] = {
    ProviderName.anthropic: "claude-opus-4-8",
    ProviderName.openai: None,
    ProviderName.local_cli: "local-cli",
}

#: Env var holding each provider's BYOK key. ``local_cli`` never reads a key (it
#: drives a local subscription CLI, not an API) — the entry is present so the
#: dict stays total for every ``ProviderName`` member.
_KEY_ENV: dict[ProviderName, str] = {
    ProviderName.anthropic: "ANTHROPIC_API_KEY",
    ProviderName.openai: "OPENAI_API_KEY",
    ProviderName.local_cli: "FORGE_LOCAL_CLI_UNUSED",
}

_TRUE = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class ModelClientConfig:
    """Immutable provider config; ``api_key``/``bridge_token`` never appear in ``repr``."""

    provider: ProviderName
    model: str
    api_key: str = field(repr=False, default="")
    effort: str = "high"
    max_tokens: int = 16000
    timeout_s: float = 600.0
    max_retries: int = 2
    base_url: str | None = None
    prompt_cache: bool = True
    cli_name: CliName | None = None
    bridge_url: str | None = None
    bridge_token: str = field(repr=False, default="")

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        detect: Callable[..., list[DetectedCli]] = detect_clis,
    ) -> ModelClientConfig | None:
        """Build a config from ``FORGE_MODEL_*`` env vars, or ``None`` if unset.

        Returns ``None`` (degrade to the offline path) when ``FORGE_MODEL_PROVIDER``
        is unset/unknown, the provider's BYOK key is absent, or no model can be
        resolved. Never raises on a missing/typo'd provider.

        When ``FORGE_MODEL_PROVIDER`` is absent, falls back to local-CLI
        auto-detection (``FORGE_LOCAL_CLI_ENABLED``); when it is explicitly set to
        ``local_cli``, that opt-in is implied by naming the provider outright.
        """
        source = os.environ if env is None else env
        raw_provider = (source.get("FORGE_MODEL_PROVIDER") or "").strip().lower()
        if not raw_provider:
            return cls._from_local_cli(source, detect)
        if raw_provider == ProviderName.local_cli.value:
            return cls._from_local_cli(source, detect, explicit=True)
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

    @classmethod
    def _from_local_cli(
        cls,
        source: Mapping[str, str],
        detect: Callable[..., list[DetectedCli]],
        *,
        explicit: bool = False,
    ) -> ModelClientConfig | None:
        """Resolve a local-CLI config, or ``None`` to keep today's fallback.

        When ``explicit`` is ``True`` (the operator set ``FORGE_MODEL_PROVIDER=local_cli``
        outright) the ``FORGE_LOCAL_CLI_ENABLED`` opt-in check is skipped — naming
        the provider IS the opt-in — but this still returns ``None`` when neither a
        bridge URL nor a detected CLI is available.
        """
        if not explicit and not _bool(source.get("FORGE_LOCAL_CLI_ENABLED"), default=False):
            return None

        bridge_url = (source.get("FORGE_CLI_BRIDGE_URL") or "").strip() or None
        preference = _preference(source.get("FORGE_LOCAL_CLI_PREFERENCE"))

        cli_name: CliName | None = None
        if bridge_url is None:
            detected = detect(preference=preference)
            if not detected:
                return None
            cli_name = detected[0].name
        elif preference:
            cli_name = preference[0]

        return cls(
            provider=ProviderName.local_cli,
            model=f"local-cli:{cli_name}" if cli_name else "local-cli",
            api_key="",
            cli_name=cli_name,
            bridge_url=bridge_url,
            bridge_token=(source.get("FORGE_CLI_BRIDGE_TOKEN") or "").strip(),
            timeout_s=_float(source.get("FORGE_LOCAL_CLI_TIMEOUT_S"), 600.0),
        )


def _preference(raw: str | None) -> list[CliName] | None:
    if not raw or not raw.strip():
        return None
    names: list[CliName] = []
    for part in raw.split(","):
        try:
            names.append(CliName(part.strip().lower()))
        except ValueError:
            continue
    return names or None


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
