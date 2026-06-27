"""Tests for the worker agent-runner task (plan Task 1.9 — single-agent loop).

Hermetic: an offline scripted model client drives the runtime; no network, no
live model provider, no Celery broker.
"""

from __future__ import annotations

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


def test_build_agent_runner_is_offline_safe() -> None:
    runner = build_agent_runner()
    result = run_objective(runner, AgentObjective(objective="Do the thing"))
    # Default scripted model finishes cleanly without any live provider call.
    assert result.status in {RunStatus.SUCCEEDED, RunStatus.ESCALATED}


def test_celery_task_is_registered() -> None:
    from forge_worker.celery_app import celery_app

    assert "forge.agent.run" in celery_app.tasks
    assert run_agent_task.name == "forge.agent.run"
