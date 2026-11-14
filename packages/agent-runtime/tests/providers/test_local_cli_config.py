"""``ModelClientConfig`` local-CLI provider resolution (Task 2, HARD-02 follow-on).

Covers the auto-detect fallback branch (``FORGE_MODEL_PROVIDER`` absent), the
explicit ``FORGE_MODEL_PROVIDER=local_cli`` opt-in path, and the namespaced
``local-cli:<cli>`` model string that keeps a subscription run from ever
matching a real API model's pricing row.
"""

from __future__ import annotations

from forge_agent.providers.config import ModelClientConfig, ProviderName
from forge_agent.providers.local_cli.detect import CliName, DetectedCli

__all__ = [
    "test_api_key_never_in_repr",
    "test_bridge_url_selects_local_cli_without_local_binary",
    "test_explicit_local_cli_provider_needs_no_optin",
    "test_explicit_local_cli_still_returns_none_without_a_cli",
    "test_explicit_provider_still_wins_over_local_cli",
    "test_local_cli_used_when_optin_and_cli_present",
    "test_model_string_is_namespaced_so_pricing_cannot_confuse_it",
    "test_no_optin_means_no_config",
    "test_optin_but_no_cli_returns_none",
]


def _detect_claude(**_kw):
    return [DetectedCli(name=CliName.claude, path="/usr/local/bin/claude")]


def _detect_none(**_kw):
    return []


def test_no_optin_means_no_config():
    """Byte-identical to today: absent opt-in, resolution still returns None."""
    assert ModelClientConfig.from_env({}, detect=_detect_claude) is None


def test_explicit_provider_still_wins_over_local_cli():
    env = {
        "FORGE_MODEL_PROVIDER": "anthropic",
        "ANTHROPIC_API_KEY": "sk-test",
        "FORGE_LOCAL_CLI_ENABLED": "1",
    }
    config = ModelClientConfig.from_env(env, detect=_detect_claude)
    assert config is not None
    assert config.provider is ProviderName.anthropic


def test_local_cli_used_when_optin_and_cli_present():
    config = ModelClientConfig.from_env({"FORGE_LOCAL_CLI_ENABLED": "1"}, detect=_detect_claude)
    assert config is not None
    assert config.provider is ProviderName.local_cli
    assert config.cli_name is CliName.claude
    assert config.api_key == ""
    assert config.model == "local-cli:claude"


def test_optin_but_no_cli_returns_none():
    assert ModelClientConfig.from_env({"FORGE_LOCAL_CLI_ENABLED": "1"}, detect=_detect_none) is None


def test_bridge_url_selects_local_cli_without_local_binary():
    config = ModelClientConfig.from_env(
        {
            "FORGE_LOCAL_CLI_ENABLED": "1",
            "FORGE_CLI_BRIDGE_URL": "http://host.docker.internal:8099",
            "FORGE_CLI_BRIDGE_TOKEN": "t0ken",
        },
        detect=_detect_none,
    )
    assert config is not None
    assert config.provider is ProviderName.local_cli
    assert config.bridge_url == "http://host.docker.internal:8099"


def test_api_key_never_in_repr():
    config = ModelClientConfig.from_env(
        {"FORGE_MODEL_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "sk-secret"}
    )
    assert "sk-secret" not in repr(config)


# --- Controller amendment A: explicit FORGE_MODEL_PROVIDER=local_cli ---


def test_explicit_local_cli_provider_needs_no_optin():
    config = ModelClientConfig.from_env(
        {"FORGE_MODEL_PROVIDER": "local_cli"}, detect=_detect_claude
    )
    assert config is not None
    assert config.provider is ProviderName.local_cli


def test_explicit_local_cli_still_returns_none_without_a_cli():
    assert (
        ModelClientConfig.from_env({"FORGE_MODEL_PROVIDER": "local_cli"}, detect=_detect_none)
        is None
    )


# --- Controller amendment B: namespaced model string ---


def test_model_string_is_namespaced_so_pricing_cannot_confuse_it():
    """A subscription run must never match an API model's pricing row."""
    config = ModelClientConfig.from_env({"FORGE_LOCAL_CLI_ENABLED": "1"}, detect=_detect_claude)
    assert config is not None
    assert config.model.startswith("local-cli:")
