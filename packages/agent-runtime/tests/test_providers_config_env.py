"""HARD-02 AC2: ``ModelClientConfig.from_env`` parses / omits correctly."""

from __future__ import annotations

from forge_agent.providers import ModelClientConfig, ProviderName

# Non-secret-shaped fake keys so the fixture never trips the secret scanner.
_ANTHROPIC_KEY = "test-anthropic-key"
_OPENAI_KEY = "test-openai-key"


def test_no_provider_returns_none() -> None:
    assert ModelClientConfig.from_env({}) is None
    # A stray key without the FORGE_MODEL_PROVIDER master switch stays offline.
    assert ModelClientConfig.from_env({"ANTHROPIC_API_KEY": _ANTHROPIC_KEY}) is None


def test_provider_without_key_returns_none() -> None:
    assert ModelClientConfig.from_env({"FORGE_MODEL_PROVIDER": "anthropic"}) is None


def test_unknown_provider_returns_none() -> None:
    assert (
        ModelClientConfig.from_env(
            {"FORGE_MODEL_PROVIDER": "gemini", "FORGE_MODEL_API_KEY": _ANTHROPIC_KEY}
        )
        is None
    )


def test_anthropic_defaults() -> None:
    config = ModelClientConfig.from_env(
        {"FORGE_MODEL_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": _ANTHROPIC_KEY}
    )
    assert config is not None
    assert config.provider is ProviderName.anthropic
    assert config.model == "claude-opus-5-5"  # reference default
    assert config.effort == "high"
    assert config.max_tokens == 16000
    assert config.prompt_cache is True
    assert config.api_key == _ANTHROPIC_KEY
    # The key never appears in repr.
    assert _ANTHROPIC_KEY not in repr(config)


def test_openai_requires_explicit_model() -> None:
    # No implicit OpenAI model default → not configured.
    assert (
        ModelClientConfig.from_env(
            {"FORGE_MODEL_PROVIDER": "openai", "OPENAI_API_KEY": _OPENAI_KEY}
        )
        is None
    )
    config = ModelClientConfig.from_env(
        {
            "FORGE_MODEL_PROVIDER": "openai",
            "OPENAI_API_KEY": _OPENAI_KEY,
            "FORGE_MODEL_NAME": "gpt-4o",
        }
    )
    assert config is not None
    assert config.provider is ProviderName.openai
    assert config.model == "gpt-4o"


def test_all_overrides_are_parsed() -> None:
    config = ModelClientConfig.from_env(
        {
            "FORGE_MODEL_PROVIDER": "anthropic",
            "ANTHROPIC_API_KEY": _ANTHROPIC_KEY,
            "FORGE_MODEL_NAME": "claude-opus-4-7",
            "FORGE_MODEL_EFFORT": "xhigh",
            "FORGE_MODEL_MAX_TOKENS": "32000",
            "FORGE_MODEL_TIMEOUT_S": "120.5",
            "FORGE_MODEL_MAX_RETRIES": "4",
            "FORGE_MODEL_BASE_URL": "https://gateway.internal/v1",
            "FORGE_MODEL_PROMPT_CACHE": "false",
        }
    )
    assert config is not None
    assert config.model == "claude-opus-4-7"
    assert config.effort == "xhigh"
    assert config.max_tokens == 32000
    assert config.timeout_s == 120.5
    assert config.max_retries == 4
    assert config.base_url == "https://gateway.internal/v1"
    assert config.prompt_cache is False


def test_bad_numeric_overrides_fall_back_to_defaults() -> None:
    config = ModelClientConfig.from_env(
        {
            "FORGE_MODEL_PROVIDER": "anthropic",
            "ANTHROPIC_API_KEY": _ANTHROPIC_KEY,
            "FORGE_MODEL_MAX_TOKENS": "not-a-number",
        }
    )
    assert config is not None
    assert config.max_tokens == 16000


def test_openai_compatible_needs_a_base_url_and_a_model_but_no_key() -> None:
    base = {"FORGE_MODEL_PROVIDER": "openai_compatible", "FORGE_MODEL_NAME": "qwen3-coder"}
    assert ModelClientConfig.from_env(base) is None
    url = "http://localhost:11434/v1"
    assert (
        ModelClientConfig.from_env(
            {"FORGE_MODEL_PROVIDER": "openai_compatible", "FORGE_MODEL_BASE_URL": url}
        )
        is None
    )
    config = ModelClientConfig.from_env({**base, "FORGE_MODEL_BASE_URL": url})
    assert config is not None
    assert config.provider is ProviderName.openai_compatible
    assert (config.model, config.base_url, config.api_key) == ("qwen3-coder", url, "")


def test_openai_compatible_never_borrows_the_openai_key() -> None:
    config = ModelClientConfig.from_env(
        {
            "FORGE_MODEL_PROVIDER": "openai_compatible",
            "FORGE_MODEL_NAME": "qwen3-coder",
            "FORGE_MODEL_BASE_URL": "http://localhost:8000/v1",
            "OPENAI_API_KEY": _OPENAI_KEY,
        }
    )
    assert config is not None
    assert config.api_key == ""
    config = ModelClientConfig.from_env(
        {
            "FORGE_MODEL_PROVIDER": "openai_compatible",
            "FORGE_MODEL_NAME": "qwen3-coder",
            "FORGE_MODEL_BASE_URL": "http://localhost:8000/v1",
            "FORGE_MODEL_API_KEY": "gateway-key",
        }
    )
    assert config is not None
    assert config.api_key == "gateway-key"


def test_local_cli_model_is_read_and_flag_shaped_values_are_dropped() -> None:
    def detect(**_: object) -> list:
        from forge_agent.providers.local_cli.detect import CliName, DetectedCli

        return [DetectedCli(name=CliName.muse, path="/usr/local/bin/muse")]

    env = {"FORGE_MODEL_PROVIDER": "local_cli", "FORGE_LOCAL_CLI_MODEL": "muse-wonder-full-1.0"}
    config = ModelClientConfig.from_env(env, detect=detect)
    assert config is not None
    assert config.cli_model == "muse-wonder-full-1.0"
    for bad in ("--yolo", "-m", "a b", ""):
        config = ModelClientConfig.from_env({**env, "FORGE_LOCAL_CLI_MODEL": bad}, detect=detect)
        assert config is not None
        assert config.cli_model is None
