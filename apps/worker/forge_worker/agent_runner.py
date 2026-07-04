"""Agent-runner task (plan Task 1.9 — single-agent loop, background half).

Runs a structured :class:`~forge_contracts.AgentObjective` through the agent
runtime's plan -> act -> observe loop. Split so it is unit-testable without Celery
or a live model provider:

* :func:`run_objective` — pure: run an objective through an injected
  :class:`~forge_agent.AgentRunner` and return its :class:`AgentRunResult`.
* :func:`build_agent_runner` — build the default runner (offline-safe scripted
  model; a real BYOK ``ModelClient`` is configured per workspace).
* :func:`run_agent_task` — the thin Celery task that builds the runner and runs.
"""

from __future__ import annotations

from typing import Any

from forge_agent import AgentRunner
from forge_agent.testing import ScriptedModelClient, finish_response
from forge_contracts import AgentObjective, AgentRunResult
from forge_worker.celery_app import celery_app
from forge_worker.reliability import ForgeTask

__all__ = [
    "build_agent_runner",
    "run_agent_task",
    "run_objective",
]


def run_objective(runner: AgentRunner, objective: AgentObjective) -> AgentRunResult:
    """Run ``objective`` through ``runner`` (plan -> act -> observe)."""
    return runner.run(objective)


def build_agent_runner() -> AgentRunner:
    """Build the default agent runner.

    Uses an offline-safe deterministic scripted model so the runtime executes
    end-to-end without any live provider call; a real BYOK ``ModelClient`` is
    injected per workspace in production.
    """
    model = ScriptedModelClient(
        responses=[],
        default=finish_response(
            "Objective acknowledged; no offline model actions were required.",
            confidence=0.9,
        ),
    )
    return AgentRunner(model)


@celery_app.task(bind=True, base=ForgeTask, name="forge.agent.run")
def run_agent_task(
    self: ForgeTask,
    objective: dict[str, Any],
    *,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Celery entrypoint: run an agent objective and return its result.

    ``idempotency_key`` (defaulting to the objective's ``task_id``) guards against
    a re-delivered / retried enqueue starting a second run: the duplicate returns
    a ``{"deduplicated": True}`` marker instead of re-invoking the model loop.
    """
    from celery.exceptions import SoftTimeLimitExceeded

    dedup_key = idempotency_key or objective.get("task_id")
    if self.is_duplicate(dedup_key):
        return {"deduplicated": True, "idempotency_key": dedup_key}
    runner = build_agent_runner()
    try:
        result = run_objective(runner, AgentObjective.model_validate(objective))
    except SoftTimeLimitExceeded:
        # A runaway loop tripped the soft time limit: escalate gracefully with a
        # structured marker instead of letting the hard kill drop the run
        # silently. ``acks_late`` means the message is not acked, so an operator
        # can re-drive it (dedup-guarded) after raising the budget.
        return {
            "escalated": True,
            "reason": "soft_time_limit_exceeded",
            "idempotency_key": dedup_key,
        }
    return result.model_dump(mode="json")
