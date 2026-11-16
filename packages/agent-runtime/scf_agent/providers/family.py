"""Which provider family a model belongs to (#173).

Two roles that must not share a model family, the red-team adversary and the
builder, or the independent reviewer and the builder, compare families here.
"""

from __future__ import annotations

from scf_agent.providers.config import ProviderName
from scf_agent.providers.router import DEFAULT_TIER_MODELS

__all__ = ["UnknownModelFamily", "provider_family", "same_family"]

#: Exact ``model -> provider`` map derived from the router's default tier tables.
_MODEL_PROVIDER: dict[str, ProviderName] = {
    model: provider for provider, tiers in DEFAULT_TIER_MODELS.items() for model in tiers.values()
}


class UnknownModelFamily(ValueError):
    """The model id names no provider family this module knows."""


def provider_family(model: str) -> ProviderName:
    """Infer the provider family of a concrete model id (deterministic, offline)."""
    key = (model or "").strip().lower()
    if not key:
        raise UnknownModelFamily("cannot determine provider: empty model id")
    if key in _MODEL_PROVIDER:
        return _MODEL_PROVIDER[key]
    if any(tok in key for tok in ("claude", "opus", "sonnet", "haiku", "fable", "anthropic")):
        return ProviderName.anthropic
    if "gpt" in key or "openai" in key or key.startswith(("o1", "o3", "o4")):
        return ProviderName.openai
    raise UnknownModelFamily(f"cannot determine provider for model {model!r}")


def same_family(first: str, second: str) -> bool:
    """Whether two model ids belong to one provider family."""
    return provider_family(first) == provider_family(second)
