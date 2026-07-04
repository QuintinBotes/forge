"""Tests for the worker agent-runner task (plan Task 1.9 — single-agent loop).

Hermetic: an offline scripted model client drives the runtime; no network, no
live model provider, no Celery broker.
"""

from __future__ import annotations

import importlib.util

import pytest

from forge_agent import AgentRunner
from forge_agent.testing import ScriptedModelClient, finish_response
from forge_contracts import AgentObjective
from forge_contracts.enums import RunStatus
from forge_worker.agent_runner import build_agent_runner, run_agent_task, run_objective


def test_run_objective_completes_offline() -> None:
    runner = AgentRunner(
        ScriptedModelClient(
            responses=[finish_response("done", confidence=0.95)],
            default=finish_response("done", confidence=0.95),
        )
    )
    result = run_objective(runner, AgentObjective(objective="Investigate the bug"))
    assert result.status == RunStatus.SUCCEEDED
    assert result.steps


def test_build_agent_runner_is_offline_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORGE_MODEL_PROVIDER", raising=False)
    runner = build_agent_runner()
    result = run_objective(runner, AgentObjective(objective="Do the thing"))
    # Default scripted model finishes cleanly without any live provider call.
    assert result.status in {RunStatus.SUCCEEDED, RunStatus.ESCALATED}


def test_build_agent_runner_no_creds_uses_scripted(monkeypatch: pytest.MonkeyPatch) -> None:
    """HARD-02 AC8: no provider creds -> the offline scripted client."""
    monkeypatch.delenv("FORGE_MODEL_PROVIDER", raising=False)
    runner = build_agent_runner()
    assert isinstance(runner._model, ScriptedModelClient)


def test_build_agent_runner_uses_injected_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """HARD-02 AC8: an injected real ``ModelClient`` is used verbatim."""
    monkeypatch.delenv("FORGE_MODEL_PROVIDER", raising=False)
    injected = ScriptedModelClient(responses=[finish_response("injected", confidence=0.9)])
    runner = build_agent_runner(model_client=injected)
    assert runner._model is injected


@pytest.mark.skipif(
    importlib.util.find_spec("anthropic") is not None,
    reason="providers extra installed — SDK-missing fallback not exercised on this lane",
)
def test_build_agent_runner_falls_back_when_sdk_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HARD-02: creds present but the provider SDK absent -> scripted, never crash."""
    monkeypatch.setenv("FORGE_MODEL_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    runner = build_agent_runner()
    assert isinstance(runner._model, ScriptedModelClient)


def test_celery_task_is_registered() -> None:
    from forge_worker.celery_app import celery_app

    assert "forge.agent.run" in celery_app.tasks
    assert run_agent_task.name == "forge.agent.run"


def test_run_agent_task_executes_and_serializes() -> None:
    # Calling the Celery task object runs the task body synchronously (no broker).
    result = run_agent_task({"objective": "do the thing"})
    assert isinstance(result, dict)
    assert result["status"] in {RunStatus.SUCCEEDED.value, RunStatus.ESCALATED.value}
    assert "steps" in result
    # The result is JSON-serialisable (mode="json"): no raw UUID/enum objects.
    import json

    json.dumps(result)
