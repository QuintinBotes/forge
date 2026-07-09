"""Unit tests for the Adaptive Orchestration model router (ao-model-router).

Covers tier -> model resolution across providers, operator overrides, the
``ModelClientConfig`` hand-off to the HARD-02 seam, the optional cheap-Haiku
tie-break classifier (with the ``ModelClient`` mocked), and the classifier
fallback path. No provider SDK, no network.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from forge_agent.providers import (
    DEFAULT_TIER_MODELS,
    MODEL_PRICING,
    ModelClientConfig,
    ModelRouter,
    ProviderName,
    RouteDecision,
    build_model_client,
    classify_tier,
    route,
)
from forge_contracts import (
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    Priority,
    TaskKind,
    TokenUsage,
)
from forge_orchestration_policy import ComplexitySizing, SizingSignals, score_complexity


# --------------------------------------------------------------------------- #
# Fake ModelClient (satisfies the frozen forge_contracts.ModelClient Protocol) #
# --------------------------------------------------------------------------- #
class FakeClassifierClient:
    """Records the request and returns a scripted ``ModelResponse`` content."""

    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls.append(request)
        return ModelResponse(
            content=self._content, usage=TokenUsage(input_tokens=0, output_tokens=1)
        )

    def stream(self, request: ModelRequest) -> Iterator[ModelStreamEvent]:  # pragma: no cover
        yield ModelStreamEvent(type="text", text=self._content)


class RaisingClient:
    """A ModelClient whose ``complete`` always raises (classifier-unavailable path)."""

    def complete(self, request: ModelRequest) -> ModelResponse:
        raise RuntimeError("provider exploded")

    def stream(self, request: ModelRequest) -> Iterator[ModelStreamEvent]:  # pragma: no cover
        yield ModelStreamEvent(type="text", text="")


def _sizing_with_score(score: int) -> ComplexitySizing:
    """A ``ComplexitySizing`` with a chosen score (tier/strategy don't matter here)."""
    tier = "junior" if score <= 6 else "medior" if score <= 16 else "senior"
    return ComplexitySizing(tier=tier, strategy="single", score=score, reasons=[])


# --------------------------------------------------------------------------- #
# Resolution                                                                   #
# --------------------------------------------------------------------------- #
class TestResolution:
    def test_anthropic_default_ladder(self) -> None:
        router = ModelRouter(provider=ProviderName.anthropic)
        assert router.resolve("junior") == "claude-haiku-4-5"
        assert router.resolve("medior") == "claude-sonnet-5"
        assert router.resolve("senior") == "claude-opus-4-8"

    def test_openai_default_ladder_is_distinct_provider(self) -> None:
        router = ModelRouter(provider=ProviderName.openai)
        assert router.resolve("junior") == "gpt-4.1-mini"
        assert router.resolve("medior") == "gpt-4.1"
        assert router.resolve("senior") == "o3"

    def test_every_default_model_is_priced(self) -> None:
        # Router defaults must be models the cost path can account for.
        for models in DEFAULT_TIER_MODELS.values():
            for model in models.values():
                assert model in MODEL_PRICING

    def test_partial_override_merges_onto_defaults(self) -> None:
        router = ModelRouter(
            provider=ProviderName.anthropic, tier_models={"senior": "claude-opus-4-7"}
        )
        assert router.resolve("senior") == "claude-opus-4-7"  # overridden
        assert router.resolve("junior") == "claude-haiku-4-5"  # inherited default

    def test_classifier_model_is_the_junior_tier(self) -> None:
        router = ModelRouter(provider=ProviderName.anthropic)
        assert router.classifier_model() == router.resolve("junior")

    def test_resolution_is_deterministic(self) -> None:
        router = ModelRouter(provider=ProviderName.anthropic)
        assert router.resolve("medior") == router.resolve("medior")


# --------------------------------------------------------------------------- #
# ModelClientConfig hand-off (reuses the HARD-02 seam, never reimplements)     #
# --------------------------------------------------------------------------- #
class TestConfigHandoff:
    def test_config_for_carries_provider_and_model(self) -> None:
        router = ModelRouter(provider=ProviderName.anthropic)
        config = router.config_for("medior", api_key="sk-fake-not-a-real-key")
        assert isinstance(config, ModelClientConfig)
        assert config.provider is ProviderName.anthropic
        assert config.model == "claude-sonnet-5"

    def test_config_overrides_flow_through(self) -> None:
        router = ModelRouter(provider=ProviderName.anthropic)
        config = router.config_for("senior", api_key="sk-fake-not-a-real-key", effort="max")
        assert config.effort == "max"

    def test_config_builds_a_real_model_client(self) -> None:
        # The resolved config drives the existing build_model_client factory
        # (adapter constructed with an injected fake SDK client — no network).
        router = ModelRouter(provider=ProviderName.anthropic)
        config = router.config_for("junior", api_key="sk-fake-not-a-real-key")
        client = build_model_client(config, client=object())
        assert client is not None


# --------------------------------------------------------------------------- #
# Validation                                                                   #
# --------------------------------------------------------------------------- #
class TestValidation:
    def test_missing_tier_without_defaults_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # With no built-in defaults for the provider, an incomplete override
        # must fail loudly rather than silently resolve a partial ladder.
        monkeypatch.setattr("forge_agent.providers.router.DEFAULT_TIER_MODELS", {})
        with pytest.raises(ValueError, match="no model configured for tier"):
            ModelRouter(provider=ProviderName.openai, tier_models={"junior": "x"})

    def test_route_requires_provider_or_router(self) -> None:
        with pytest.raises(ValueError, match="requires either provider or router"):
            route(_sizing_with_score(2))

    def test_route_rejects_conflicting_provider_and_router(self) -> None:
        router = ModelRouter(provider=ProviderName.anthropic)
        with pytest.raises(ValueError, match="conflicts with router"):
            route(_sizing_with_score(2), provider=ProviderName.openai, router=router)


# --------------------------------------------------------------------------- #
# route(): heuristic-only path                                                 #
# --------------------------------------------------------------------------- #
class TestRouteHeuristic:
    def test_clear_junior_routes_to_junior_model(self) -> None:
        sizing = _sizing_with_score(2)  # well inside junior, not a boundary
        decision = route(sizing, provider=ProviderName.anthropic)
        assert isinstance(decision, RouteDecision)
        assert decision.tier == "junior"
        assert decision.model == "claude-haiku-4-5"
        assert decision.used_classifier is False

    def test_clear_senior_routes_to_senior_model(self) -> None:
        sizing = _sizing_with_score(30)
        decision = route(sizing, provider=ProviderName.openai)
        assert decision.tier == "senior"
        assert decision.model == "o3"

    def test_real_sizing_flows_through(self) -> None:
        sizing = score_complexity(SizingSignals(kind=TaskKind.DOC, priority=Priority.LOW))
        decision = route(sizing, provider=ProviderName.anthropic)
        assert decision.tier == sizing.tier
        assert decision.provider is ProviderName.anthropic

    def test_boundary_without_classifier_keeps_heuristic_tier(self) -> None:
        sizing = _sizing_with_score(6)  # junior/medior boundary
        decision = route(sizing, provider=ProviderName.anthropic)
        assert decision.used_classifier is False
        assert decision.tier == "junior"  # heuristic tier for score 6
        assert any("no classifier" in r for r in decision.reasons)


# --------------------------------------------------------------------------- #
# route(): classifier tie-break path (ModelClient mocked)                      #
# --------------------------------------------------------------------------- #
class TestRouteClassifier:
    def test_classifier_not_consulted_when_not_a_boundary(self) -> None:
        client = FakeClassifierClient("senior")
        sizing = _sizing_with_score(2)  # clear junior, no tie
        decision = route(sizing, provider=ProviderName.anthropic, classifier=client)
        assert decision.used_classifier is False
        assert decision.tier == "junior"
        assert client.calls == []  # cheap model never called on a non-tie

    def test_classifier_breaks_tie_upward(self) -> None:
        client = FakeClassifierClient("medior")
        sizing = _sizing_with_score(6)  # junior/medior boundary -> candidates junior,medior
        decision = route(
            sizing, provider=ProviderName.anthropic, classifier=client, summary="add a subsystem"
        )
        assert decision.used_classifier is True
        assert decision.tier == "medior"
        assert decision.model == "claude-sonnet-5"
        assert len(client.calls) == 1
        assert any("broke tie" in r for r in decision.reasons)

    def test_classifier_can_confirm_heuristic_tier(self) -> None:
        client = FakeClassifierClient("junior")
        sizing = _sizing_with_score(7)  # boundary, heuristic tier = medior
        decision = route(sizing, provider=ProviderName.anthropic, classifier=client)
        # classifier picked junior (a valid candidate) -> tie broken downward
        assert decision.tier == "junior"
        assert decision.used_classifier is True

    def test_classifier_error_falls_back_to_heuristic(self) -> None:
        sizing = _sizing_with_score(16)  # medior/senior boundary, heuristic = medior
        decision = route(sizing, provider=ProviderName.anthropic, classifier=RaisingClient())
        assert decision.used_classifier is True
        assert decision.tier == "medior"  # fell back to heuristic
        assert decision.model == "claude-sonnet-5"

    def test_classifier_ambiguous_answer_falls_back(self) -> None:
        # Reply naming BOTH candidates is unusable -> fallback to heuristic tier.
        client = FakeClassifierClient("could be junior or medior honestly")
        sizing = _sizing_with_score(6)
        decision = route(sizing, provider=ProviderName.anthropic, classifier=client)
        assert decision.tier == "junior"

    def test_classifier_out_of_range_answer_falls_back(self) -> None:
        client = FakeClassifierClient("senior")  # not a candidate at the junior/medior tie
        sizing = _sizing_with_score(6)
        decision = route(sizing, provider=ProviderName.anthropic, classifier=client)
        assert decision.tier == "junior"


# --------------------------------------------------------------------------- #
# classify_tier() unit-level                                                   #
# --------------------------------------------------------------------------- #
class TestClassifyTier:
    def test_single_candidate_short_circuits_without_calling(self) -> None:
        client = FakeClassifierClient("senior")
        result = classify_tier(client, candidates=["medior"], fallback="medior")
        assert result == "medior"
        assert client.calls == []

    def test_request_is_seeded_with_system_and_summary(self) -> None:
        client = FakeClassifierClient("medior")
        classify_tier(
            client, candidates=["junior", "medior"], fallback="junior", summary="Refactor auth"
        )
        request = client.calls[0]
        assert request.system is not None and "tier" in request.system.lower()
        assert "Refactor auth" in request.messages[0].content
        assert "junior" in request.messages[0].content and "medior" in request.messages[0].content

    def test_case_insensitive_match(self) -> None:
        client = FakeClassifierClient("  MEDIOR\n")
        result = classify_tier(client, candidates=["junior", "medior"], fallback="junior")
        assert result == "medior"

    def test_empty_answer_falls_back(self) -> None:
        client = FakeClassifierClient("")
        result = classify_tier(client, candidates=["junior", "medior"], fallback="junior")
        assert result == "junior"
