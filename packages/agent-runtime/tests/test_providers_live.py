"""HARD-02 AC9-AC12: live BYOK model round-trip (creds-gated, opt-in).

These drive a REAL provider over the network using an env BYOK key. They are
marked ``integration`` + ``live_model`` and **skip cleanly** when the provider,
key, or SDK extra is absent — the default ``uv run pytest -q`` lane stays
hermetic and never falls back to a fake on this lane.

Run them (once real creds exist):

    uv sync --extra providers
    cp .env.integration.example .env.integration   # then fill ANTHROPIC_API_KEY/OPENAI_API_KEY
    set -a && source .env.integration && set +a
    export FORGE_MODEL_PROVIDER=anthropic FORGE_MODEL_MAX_TOKENS=1024
    uv run pytest -m live_model packages/agent-runtime -q

See docs/runbooks/live-model.md.
"""

from __future__ import annotations

import importlib.util
import os

import pytest

from forge_agent import AgentRunner
from forge_agent.providers import ModelClientConfig, ProviderName, build_model_client
from forge_contracts import AgentObjective, RunStatus

pytestmark = [pytest.mark.integration, pytest.mark.live_model]

_SDK_FOR = {ProviderName.anthropic: "anthropic", ProviderName.openai: "openai"}
_REDACT_MATCHING = ("sk-", "Bearer ", "ghp_", "AKIA")


def _config_for(provider: ProviderName) -> ModelClientConfig | None:
    """Build a config for ``provider`` from env, or ``None`` if unavailable."""
    env = dict(os.environ)
    env["FORGE_MODEL_PROVIDER"] = provider.value
    env.setdefault("FORGE_MODEL_MAX_TOKENS", "1024")
    config = ModelClientConfig.from_env(env)
    if config is None:
        return None
    if importlib.util.find_spec(_SDK_FOR[provider]) is None:
        return None
    return config


def _configured_provider() -> ProviderName | None:
    raw = (os.environ.get("FORGE_MODEL_PROVIDER") or "").strip().lower()
    try:
        return ProviderName(raw)
    except ValueError:
        return None


def _minimal_objective() -> AgentObjective:
    return AgentObjective(
        objective="Reply with the single word DONE and finish. Do not use any tools.",
        model=None,
    )


def _skip_unless(provider: ProviderName | None) -> ModelClientConfig:
    if provider is None:
        pytest.skip(
            "FORGE_MODEL_PROVIDER not set (anthropic|openai) — see docs/runbooks/live-model.md"
        )
    config = _config_for(provider)
    if config is None:
        pytest.skip(
            f"live model creds/SDK absent for provider={provider.value!r}: set the BYOK key "
            f"and install forge-agent[providers]; see docs/runbooks/live-model.md"
        )
    return config


def _key_material() -> list[str]:
    return [v for k, v in os.environ.items() if v and ("API_KEY" in k or "MODEL_API_KEY" in k)]


def test_live_agent_run_reaches_terminal_state() -> None:
    config = _skip_unless(_configured_provider())
    runner = AgentRunner(build_model_client(config))
    result = runner.run(_minimal_objective())
    assert result.status in {RunStatus.SUCCEEDED, RunStatus.ESCALATED}
    # The real StateGraph executed: at least a planning step with model output.
    assert result.steps
    assert result.artifacts.get("iterations") is not None


def test_live_usage_recorded() -> None:
    config = _skip_unless(_configured_provider())
    runner = AgentRunner(build_model_client(config))
    result = runner.run(_minimal_objective())
    usage = result.artifacts["model_usage"]
    assert usage["input_tokens"] > 0
    assert usage["cost_usd"] >= 0.0
    assert usage["calls"] >= 1


def test_live_redaction_holds() -> None:
    config = _skip_unless(_configured_provider())
    runner = AgentRunner(build_model_client(config))
    result = runner.run(_minimal_objective())
    secrets = _key_material()
    blob = result.model_dump_json()
    for secret in secrets:
        assert secret not in blob, "a BYOK key leaked into the run trace"


@pytest.mark.parametrize("provider", list(ProviderName))
def test_live_provider_swap(provider: ProviderName) -> None:
    config = _config_for(provider)
    if config is None:
        pytest.skip(f"provider {provider.value!r} not configured — swap coverage skipped")
    runner = AgentRunner(build_model_client(config))
    result = runner.run(_minimal_objective())
    assert result.status in {RunStatus.SUCCEEDED, RunStatus.ESCALATED}
