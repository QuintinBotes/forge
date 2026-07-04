"""HARD-02 AC9 (worker lane): the agent task runs against a real BYOK provider.

Creds-gated + SDK-gated. Skips cleanly when ``FORGE_MODEL_PROVIDER`` / the BYOK
key / the ``providers`` extra are absent, so the default ``uv run pytest -q`` lane
stays hermetic and network-free. See docs/runbooks/live-model.md.
"""

from __future__ import annotations

import importlib.util
import os

import pytest

from forge_agent.testing import ScriptedModelClient
from forge_contracts.enums import RunStatus
from forge_worker.agent_runner import build_agent_runner, run_agent_task

pytestmark = [pytest.mark.integration, pytest.mark.live_model]

_SDK_FOR = {"anthropic": "anthropic", "openai": "openai"}


def _skip_unless_configured() -> None:
    provider = (os.environ.get("FORGE_MODEL_PROVIDER") or "").strip().lower()
    if provider not in _SDK_FOR:
        pytest.skip(
            "FORGE_MODEL_PROVIDER not set (anthropic|openai); see docs/runbooks/live-model.md"
        )
    key = os.environ.get("ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY")
    if not (key or os.environ.get("FORGE_MODEL_API_KEY")):
        pytest.skip(f"BYOK key absent for provider={provider!r}; see docs/runbooks/live-model.md")
    if importlib.util.find_spec(_SDK_FOR[provider]) is None:
        pytest.skip("providers extra not installed (uv sync --extra providers)")


def test_build_agent_runner_resolves_real_client() -> None:
    _skip_unless_configured()
    runner = build_agent_runner()
    # A real provider client (not the offline scripted default) was resolved.
    assert not isinstance(runner._model, ScriptedModelClient)


def test_live_agent_task_reaches_terminal_state() -> None:
    _skip_unless_configured()
    result = run_agent_task(
        {"objective": "Reply with the single word DONE and finish. Do not use any tools."}
    )
    assert result["status"] in {RunStatus.SUCCEEDED.value, RunStatus.ESCALATED.value}
    usage = result["artifacts"]["model_usage"]
    assert usage["input_tokens"] > 0
