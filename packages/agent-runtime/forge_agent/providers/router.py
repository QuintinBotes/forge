"""Provider-agnostic tier -> model router (Adaptive Orchestration: ao-model-router).

The :class:`ModelRouter` maps an Adaptive Orchestration seniority ``tier``
(``junior``/``medior``/``senior`` from :mod:`forge_orchestration_policy`) to a
concrete model string for the workspace's **BYOK provider**, then hands off to
the existing HARD-02 model client (:class:`ModelClientConfig` +
:func:`build_model_client`) — it never reimplements a model call or hardcodes a
single provider. The default map follows the approved Adaptive Orchestration
decision (Claude: junior=Haiku, medior=Sonnet, senior=Opus) and ships an
equivalent OpenAI map so the router stays provider-agnostic; operators may
override any tier per provider.

When the deterministic heuristic sizing lands on a tier boundary
(:func:`forge_orchestration_policy.candidate_tiers` returns two adjacent tiers),
an *optional* cheap-Haiku LLM classifier can break the tie via
:func:`classify_tier`. The classifier consumes the frozen
``forge_contracts.ModelClient`` Protocol (mocked in tests) and always falls back
to the heuristic tier when it is absent, errors, or returns an unusable answer —
so routing is never blocked on a live model call.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from forge_agent.providers.config import ModelClientConfig, ProviderName
from forge_contracts import ModelClient, ModelMessage, ModelRequest
from forge_orchestration_policy import ComplexitySizing, Tier, candidate_tiers

__all__ = [
    "DEFAULT_TIER_MODELS",
    "ModelRouter",
    "RouteDecision",
    "classify_tier",
    "route",
]

#: Default ``tier -> model`` map **per provider**. Anthropic tracks the approved
#: Adaptive Orchestration decision (junior=Haiku, medior=Sonnet, senior=Opus);
#: OpenAI carries an equivalent cheap/mid/frontier ladder so the router never
#: assumes a single provider. Operators override via ``ModelRouter(tier_models=…)``
#: (a partial override merges onto these defaults).
DEFAULT_TIER_MODELS: dict[ProviderName, dict[Tier, str]] = {
    ProviderName.anthropic: {
        "junior": "claude-haiku-4-5",
        "medior": "claude-sonnet-5",
        "senior": "claude-opus-4-8",
    },
    ProviderName.openai: {
        "junior": "gpt-4.1-mini",
        "medior": "gpt-4.1",
        "senior": "o3",
    },
}

#: System prompt for the tie-break classifier. Kept terse + deterministic so the
#: cheap model answers with a single tier token.
_CLASSIFIER_SYSTEM = (
    "You size software engineering tasks into a seniority tier for an autonomous "
    "coding agent. Reply with EXACTLY ONE word — one of the allowed tiers — and "
    "nothing else. junior = small, well-scoped, low-risk; medior = moderate "
    "scope or some ambiguity; senior = large, cross-cutting, high-risk, or "
    "underspecified."
)


@dataclass(frozen=True)
class ModelRouter:
    """Resolve a :class:`~forge_orchestration_policy.Tier` to a concrete model.

    ``tier_models`` is merged onto :data:`DEFAULT_TIER_MODELS` for ``provider``,
    so a caller may override only the tiers it cares about and still resolve the
    rest. Providers without a built-in default must supply a complete map.
    """

    provider: ProviderName
    tier_models: Mapping[Tier, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        base = DEFAULT_TIER_MODELS.get(self.provider, {})
        merged: dict[Tier, str] = {**base, **dict(self.tier_models)}
        missing = [t for t in ("junior", "medior", "senior") if t not in merged]
        if missing:
            raise ValueError(
                f"no model configured for tier(s) {missing} on provider "
                f"{self.provider.value!r}; supply them via tier_models"
            )
        # Freeze the fully-resolved map (bypass frozen-dataclass immutability once).
        object.__setattr__(self, "tier_models", merged)

    def resolve(self, tier: Tier) -> str:
        """Return the concrete model string for ``tier`` on this provider."""
        try:
            return self.tier_models[tier]
        except KeyError:  # pragma: no cover - guarded by __post_init__
            raise ValueError(f"unknown tier: {tier!r}") from None

    def classifier_model(self) -> str:
        """The cheap model to run the tie-break classifier with (the junior tier)."""
        return self.resolve("junior")

    def config_for(self, tier: Tier, *, api_key: str, **overrides: Any) -> ModelClientConfig:
        """Build a :class:`ModelClientConfig` for ``tier`` (feeds ``build_model_client``).

        Reuses the HARD-02 config/adapter seam verbatim — the router only picks
        the model string; every other client knob (effort, timeouts, retries)
        comes from ``ModelClientConfig`` defaults or ``overrides``.
        """
        return ModelClientConfig(
            provider=self.provider,
            model=self.resolve(tier),
            api_key=api_key,
            **overrides,
        )


@dataclass(frozen=True)
class RouteDecision:
    """The resolved routing decision for a sizing (spec: ao-model-router)."""

    tier: Tier
    model: str
    provider: ProviderName
    used_classifier: bool
    reasons: list[str] = field(default_factory=list)


def classify_tier(
    client: ModelClient,
    *,
    candidates: list[Tier],
    fallback: Tier,
    summary: str = "",
) -> Tier:
    """Break a heuristic tie by asking a cheap model to pick one of ``candidates``.

    Calls ``client.complete`` (mocked in tests) with a terse, deterministic
    prompt and returns the single candidate tier named in the reply. Falls back
    to ``fallback`` whenever the classifier is unusable — an empty/ambiguous
    answer, an answer outside ``candidates``, or **any** exception from the
    provider — so a routing decision is never blocked on the model call.
    """
    if len(candidates) < 2:
        return fallback
    allowed = ", ".join(candidates)
    prompt = (
        f"Allowed tiers: {allowed}.\n"
        f"Task summary:\n{summary.strip() or '(no summary provided)'}\n\n"
        "Which tier fits best? Answer with one word."
    )
    request = ModelRequest(
        model="",  # the injected client carries its own (cheap) model; this is ignored
        system=_CLASSIFIER_SYSTEM,
        messages=[ModelMessage(role="user", content=prompt)],
        max_tokens=8,
    )
    try:
        response = client.complete(request)
    except Exception:
        return fallback
    return _parse_tier(getattr(response, "content", "") or "", candidates, fallback)


def _parse_tier(content: str, candidates: list[Tier], fallback: Tier) -> Tier:
    """Return the single ``candidates`` tier named in ``content`` (else ``fallback``)."""
    text = content.strip().lower()
    matched = [tier for tier in candidates if tier in text]
    if len(matched) == 1:
        return matched[0]
    return fallback


def route(
    sizing: ComplexitySizing,
    *,
    provider: ProviderName | None = None,
    router: ModelRouter | None = None,
    classifier: ModelClient | None = None,
    summary: str = "",
) -> RouteDecision:
    """Resolve ``sizing`` to a :class:`RouteDecision` for the workspace's provider.

    Supply either ``provider`` (uses the default tier map) or a pre-built
    ``router`` (for operator overrides). When the heuristic sizing is on a tier
    boundary and a ``classifier`` is provided, the classifier breaks the tie;
    otherwise the deterministic ``sizing.tier`` is used unchanged.
    """
    if router is None:
        if provider is None:
            raise ValueError("route() requires either provider or router")
        router = ModelRouter(provider=provider)
    elif provider is not None and provider is not router.provider:
        raise ValueError(
            f"provider {provider.value!r} conflicts with router.provider {router.provider.value!r}"
        )

    candidates = candidate_tiers(sizing)
    tier: Tier = sizing.tier
    used_classifier = False
    reasons = [f"heuristic tier={sizing.tier} (score={sizing.score})"]

    if len(candidates) > 1 and classifier is not None:
        tier = classify_tier(
            classifier, candidates=candidates, fallback=sizing.tier, summary=summary
        )
        used_classifier = True
        if tier == sizing.tier:
            reasons.append(f"classifier kept tier={tier} (candidates={candidates})")
        else:
            reasons.append(f"classifier broke tie -> tier={tier} (candidates={candidates})")
    elif len(candidates) > 1:
        reasons.append(f"tie candidates={candidates}; no classifier, kept heuristic tier")

    model = router.resolve(tier)
    reasons.append(f"provider={router.provider.value} tier={tier} -> model={model}")
    return RouteDecision(
        tier=tier,
        model=model,
        provider=router.provider,
        used_classifier=used_classifier,
        reasons=reasons,
    )
