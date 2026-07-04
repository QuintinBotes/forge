"""HARD-02 AC7: a provider exception carrying a secret is redacted on re-raise.

Uses the real ``forge_api.observability.redaction.redact_text`` as the injected
redactor (the same filter the worker/API wire), so an SDK error that echoes a
``sk-…`` / ``Bearer …`` token surfaces as ``[REDACTED]`` in the ``ModelClientError``
— never the raw secret, and never a request/response body in the logs.
"""

from __future__ import annotations

import logging

import pytest
from _provider_fakes import FakeAnthropicSDK, FakeOpenAISDK

from forge_agent.providers import (
    AnthropicModelClient,
    ModelClientError,
    OpenAIModelClient,
)
from forge_api.observability.redaction import redact_text
from forge_contracts import ModelRequest

# Obviously-fake, secret-shaped values used only to prove redaction (allowlisted
# in .gitleaks.toml). Never a real credential.
_FAKE_KEY = "sk-abcdef0123456789abcdef0123"
_FAKE_BEARER = "Bearer abcdef0123456789abcdef0123"


def test_anthropic_error_is_redacted() -> None:
    sdk = FakeAnthropicSDK(error=RuntimeError(f"401 auth failed for {_FAKE_KEY}"))
    client = AnthropicModelClient(
        model="claude-opus-4-8", api_key="k", redactor=redact_text, client=sdk
    )
    with pytest.raises(ModelClientError) as excinfo:
        client.complete(ModelRequest(model="claude-opus-4-8"))
    message = str(excinfo.value)
    assert _FAKE_KEY not in message
    assert "[REDACTED]" in message


def test_openai_error_is_redacted() -> None:
    sdk = FakeOpenAISDK(error=RuntimeError(f"invalid auth header {_FAKE_BEARER}"))
    client = OpenAIModelClient(model="gpt-4o", api_key="k", redactor=redact_text, client=sdk)
    with pytest.raises(ModelClientError) as excinfo:
        client.complete(ModelRequest(model="gpt-4o"))
    message = str(excinfo.value)
    assert "abcdef0123456789abcdef0123" not in message
    assert "[REDACTED]" in message


def test_adapter_logs_no_secret_on_error(caplog: pytest.LogCaptureFixture) -> None:
    sdk = FakeAnthropicSDK(error=RuntimeError(f"boom {_FAKE_KEY}"))
    client = AnthropicModelClient(
        model="claude-opus-4-8", api_key="k", redactor=redact_text, client=sdk
    )
    with (
        caplog.at_level(logging.DEBUG, logger="forge_agent.providers.anthropic"),
        pytest.raises(ModelClientError),
    ):
        client.complete(ModelRequest(model="claude-opus-4-8"))
    assert _FAKE_KEY not in caplog.text
