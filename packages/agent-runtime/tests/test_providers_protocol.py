"""HARD-02 AC1/AC2: both adapters satisfy the frozen Protocol; factory routing.

Hermetic — the SDK is injected via ``client=`` (a fake), so neither ``anthropic``
nor ``openai`` needs to be installed.
"""

from __future__ import annotations

import pytest
from _provider_fakes import FakeAnthropicSDK, FakeOpenAISDK, anthropic_message, openai_completion

from forge_agent.providers import (
    AnthropicModelClient,
    ModelClientConfig,
    ModelClientError,
    OpenAIModelClient,
    ProviderName,
    build_model_client,
)
from forge_contracts import ModelClient, ModelRequest


def _anthropic() -> AnthropicModelClient:
    sdk = FakeAnthropicSDK(message=anthropic_message(text="hello", input_tokens=3, output_tokens=2))
    return AnthropicModelClient(model="claude-opus-4-8", api_key="test-key", client=sdk)


def _openai() -> OpenAIModelClient:
    sdk = FakeOpenAISDK(
        completion=openai_completion(content="hi", prompt_tokens=3, completion_tokens=2)
    )
    return OpenAIModelClient(model="gpt-4o", api_key="test-key", client=sdk)


def test_both_adapters_are_model_clients() -> None:
    assert isinstance(_anthropic(), ModelClient)
    assert isinstance(_openai(), ModelClient)


def test_anthropic_complete_and_stream() -> None:
    client = _anthropic()
    response = client.complete(ModelRequest(model="claude-opus-4-8"))
    assert response.content == "hello"
    assert response.usage is not None and response.usage.input_tokens == 3

    sdk = FakeAnthropicSDK(text_chunks=["he", "llo"])
    streaming = AnthropicModelClient(model="claude-opus-4-8", api_key="k", client=sdk)
    events = list(streaming.stream(ModelRequest(model="claude-opus-4-8")))
    assert [e.text for e in events] == ["he", "llo"]
    assert all(e.type == "text" for e in events)


def test_openai_complete_and_stream() -> None:
    client = _openai()
    response = client.complete(ModelRequest(model="gpt-4o"))
    assert response.content == "hi"
    assert response.usage is not None and response.usage.output_tokens == 2

    sdk = FakeOpenAISDK(events=["h", "i"])
    streaming = OpenAIModelClient(model="gpt-4o", api_key="k", client=sdk)
    events = list(streaming.stream(ModelRequest(model="gpt-4o")))
    assert [e.text for e in events] == ["h", "i"]


def test_anthropic_honors_per_request_model() -> None:
    # Adaptive Orchestration routes a per-role model via request.model; the client
    # must send THAT model, not its constructor-bound default.
    sdk = FakeAnthropicSDK(message=anthropic_message(text="ok"))
    client = AnthropicModelClient(model="claude-opus-4-8", api_key="k", client=sdk)
    client.complete(ModelRequest(model="claude-haiku-4-5"))
    assert sdk.captured_kwargs[0]["model"] == "claude-haiku-4-5"


def test_anthropic_falls_back_to_bound_model_when_request_model_unset() -> None:
    # An empty request.model keeps single-agent callers pinned to the bound model.
    sdk = FakeAnthropicSDK(message=anthropic_message(text="ok"))
    client = AnthropicModelClient(model="claude-opus-4-8", api_key="k", client=sdk)
    client.complete(ModelRequest(model=""))
    assert sdk.captured_kwargs[0]["model"] == "claude-opus-4-8"


def test_openai_honors_per_request_model() -> None:
    sdk = FakeOpenAISDK(completion=openai_completion(content="ok"))
    client = OpenAIModelClient(model="gpt-4o", api_key="k", client=sdk)
    client.complete(ModelRequest(model="gpt-4.1-mini"))
    assert sdk.captured_kwargs[0]["model"] == "gpt-4.1-mini"


def test_openai_falls_back_to_bound_model_when_request_model_unset() -> None:
    sdk = FakeOpenAISDK(completion=openai_completion(content="ok"))
    client = OpenAIModelClient(model="gpt-4o", api_key="k", client=sdk)
    client.complete(ModelRequest(model=""))
    assert sdk.captured_kwargs[0]["model"] == "gpt-4o"


def test_factory_routes_by_provider() -> None:
    anthropic_cfg = ModelClientConfig(
        provider=ProviderName.anthropic, model="claude-opus-4-8", api_key="k"
    )
    openai_cfg = ModelClientConfig(provider=ProviderName.openai, model="gpt-4o", api_key="k")
    assert isinstance(
        build_model_client(anthropic_cfg, client=FakeAnthropicSDK()), AnthropicModelClient
    )
    assert isinstance(build_model_client(openai_cfg, client=FakeOpenAISDK()), OpenAIModelClient)


def test_factory_unknown_provider_raises() -> None:
    class _Bogus:
        provider = "gemini"

    with pytest.raises(ModelClientError):
        build_model_client(_Bogus())  # type: ignore[arg-type]
