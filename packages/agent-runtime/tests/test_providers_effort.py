"""ao-effort: map AO effort levels -> provider reasoning knobs in the ModelClient.

The five roles' ao-config (:class:`forge_contracts.orchestration_config.Effort`:
``low``/``medium``/``high``/``max``) selects how hard a model should think. Each
provider exposes a *different* knob:

* Anthropic (Claude): ``output_config.effort`` — the extended-thinking depth
  control (``budget_tokens`` is removed on Opus 4.7/4.8 and 400s, so ``effort`` is
  the current knob). Claude accepts ``low``/``medium``/``high``/``xhigh``/``max``,
  so the four AO levels pass through unchanged.
* OpenAI: ``reasoning_effort`` — accepts ``low``/``medium``/``high`` (no ``max``);
  AO ``max`` clamps to ``high``. Only set on reasoning models.

These tests exercise the mapping (unit), its use in the pure translators, the two
adapters over a mocked SDK client, and the per-role wiring from ao-config.
"""

from __future__ import annotations

import pytest
from _provider_fakes import FakeAnthropicSDK, FakeOpenAISDK

from forge_agent.providers import (
    AnthropicModelClient,
    OpenAIModelClient,
    ProviderName,
    build_model_client,
    translate,
)
from forge_agent.providers import (
    effort as effort_map,
)
from forge_agent.providers.config import ModelClientConfig
from forge_agent.providers.router import ModelRouter
from forge_contracts import ModelMessage, ModelRequest
from forge_contracts.orchestration_config import Effort

_REQUEST = ModelRequest(
    model="",
    system="You are Forge.",
    messages=[ModelMessage(role="user", content="do the thing")],
)


# --------------------------------------------------------------------------- #
# The AO levels are exactly the ao-config Effort enum.                          #
# --------------------------------------------------------------------------- #


def test_ao_levels_are_the_ao_config_effort_enum() -> None:
    assert tuple(e.value for e in Effort) == effort_map.AO_EFFORT_LEVELS


# --------------------------------------------------------------------------- #
# Anthropic knob mapping                                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("level", ["low", "medium", "high", "max"])
def test_anthropic_effort_passes_ao_levels_through(level: str) -> None:
    assert effort_map.anthropic_effort(level) == level


def test_anthropic_effort_passes_xhigh_through() -> None:
    # xhigh is a valid Claude effort (Opus 4.7/4.8) even though it is not an AO level.
    assert effort_map.anthropic_effort("xhigh") == "xhigh"


def test_anthropic_effort_unknown_falls_back_to_high() -> None:
    assert effort_map.anthropic_effort("turbo") == "high"
    assert effort_map.anthropic_effort("") == "high"


def test_anthropic_effort_accepts_effort_enum() -> None:
    assert effort_map.anthropic_effort(Effort.MAX) == "max"
    assert effort_map.anthropic_effort(Effort.LOW) == "low"


# --------------------------------------------------------------------------- #
# OpenAI knob mapping                                                           #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("level", "expected"),
    [("low", "low"), ("medium", "medium"), ("high", "high"), ("max", "high")],
)
def test_openai_reasoning_effort_maps_each_ao_level(level: str, expected: str) -> None:
    # OpenAI reasoning_effort has no "max" — it clamps to "high".
    assert effort_map.openai_reasoning_effort(level) == expected


def test_openai_reasoning_effort_accepts_effort_enum_and_falls_back() -> None:
    assert effort_map.openai_reasoning_effort(Effort.MAX) == "high"
    assert effort_map.openai_reasoning_effort("turbo") == "high"


@pytest.mark.parametrize("model", ["o3", "o1-mini", "o4-mini", "gpt-5", "gpt-5-mini"])
def test_openai_reasoning_models_support_reasoning_effort(model: str) -> None:
    assert effort_map.openai_supports_reasoning_effort(model) is True


@pytest.mark.parametrize("model", ["gpt-4.1", "gpt-4.1-mini", "gpt-4o", ""])
def test_openai_non_reasoning_models_do_not_support_reasoning_effort(model: str) -> None:
    assert effort_map.openai_supports_reasoning_effort(model) is False


# --------------------------------------------------------------------------- #
# The pure translators apply the mapping.                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("level", ["low", "medium", "high", "max"])
def test_anthropic_stream_kwargs_sets_mapped_output_effort(level: str) -> None:
    kwargs = translate.anthropic_stream_kwargs(
        _REQUEST, model="claude-opus-4-8", effort=level, max_tokens=16000, prompt_cache=True
    )
    assert kwargs["output_config"] == {"effort": level}


def test_anthropic_stream_kwargs_maps_unknown_effort_to_high() -> None:
    kwargs = translate.anthropic_stream_kwargs(
        _REQUEST, model="claude-opus-4-8", effort="turbo", max_tokens=16000, prompt_cache=True
    )
    assert kwargs["output_config"] == {"effort": "high"}


@pytest.mark.parametrize(
    ("level", "expected"),
    [("low", "low"), ("medium", "medium"), ("high", "high"), ("max", "high")],
)
def test_openai_create_kwargs_sets_reasoning_effort_on_reasoning_model(
    level: str, expected: str
) -> None:
    kwargs = translate.openai_create_kwargs(_REQUEST, model="o3", max_tokens=8000, effort=level)
    assert kwargs["reasoning_effort"] == expected


def test_openai_create_kwargs_omits_reasoning_effort_on_non_reasoning_model() -> None:
    kwargs = translate.openai_create_kwargs(_REQUEST, model="gpt-4o", max_tokens=8000, effort="max")
    assert "reasoning_effort" not in kwargs


# --------------------------------------------------------------------------- #
# The adapters set the right knob over a mocked SDK client.                     #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("level", ["low", "medium", "high", "max"])
def test_anthropic_adapter_sets_output_effort_per_level(level: str) -> None:
    sdk = FakeAnthropicSDK()
    client = AnthropicModelClient(model="claude-opus-4-8", api_key="k", effort=level, client=sdk)
    client.complete(_REQUEST)
    assert sdk.captured_kwargs[0]["output_config"] == {"effort": level}


def test_anthropic_adapter_accepts_effort_enum_from_ao_config() -> None:
    sdk = FakeAnthropicSDK()
    client = AnthropicModelClient(
        model="claude-opus-4-8", api_key="k", effort=Effort.MAX, client=sdk
    )
    client.complete(_REQUEST)
    assert sdk.captured_kwargs[0]["output_config"] == {"effort": "max"}


@pytest.mark.parametrize(
    ("level", "expected"),
    [("low", "low"), ("medium", "medium"), ("high", "high"), ("max", "high")],
)
def test_openai_adapter_sets_reasoning_effort_per_level(level: str, expected: str) -> None:
    sdk = FakeOpenAISDK()
    client = OpenAIModelClient(model="o3", api_key="k", effort=level, client=sdk)
    client.complete(_REQUEST)
    assert sdk.captured_kwargs[0]["reasoning_effort"] == expected


def test_openai_adapter_omits_reasoning_effort_on_non_reasoning_model() -> None:
    sdk = FakeOpenAISDK()
    client = OpenAIModelClient(model="gpt-4o", api_key="k", effort="max", client=sdk)
    client.complete(_REQUEST)
    assert "reasoning_effort" not in sdk.captured_kwargs[0]


# --------------------------------------------------------------------------- #
# Per-role from ao-config: RoleExecution.effort -> ModelClientConfig -> knob.   #
# --------------------------------------------------------------------------- #


def test_role_effort_flows_through_router_config_to_anthropic_knob() -> None:
    # The router builds a ModelClientConfig for a tier; the per-role ao-config
    # Effort rides in as the `effort` override and reaches output_config.effort.
    router = ModelRouter(provider=ProviderName.anthropic)
    config = router.config_for("senior", api_key="k", effort=Effort.MAX)
    assert isinstance(config, ModelClientConfig)
    assert config.effort is Effort.MAX

    sdk = FakeAnthropicSDK()
    client = build_model_client(config, client=sdk)
    client.complete(_REQUEST)
    assert sdk.captured_kwargs[0]["output_config"] == {"effort": "max"}


def test_role_effort_flows_through_router_config_to_openai_knob() -> None:
    router = ModelRouter(provider=ProviderName.openai)
    # senior OpenAI default is a reasoning model (o3) -> reasoning_effort is set.
    config = router.config_for("senior", api_key="k", effort=Effort.LOW)

    sdk = FakeOpenAISDK()
    client = build_model_client(config, client=sdk)
    client.complete(_REQUEST)
    assert sdk.captured_kwargs[0]["reasoning_effort"] == "low"
