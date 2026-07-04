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

import uuid
from collections.abc import Callable
from typing import Any

from forge_agent import AgentRunner
from forge_agent.providers import (
    ModelClientConfig,
    ModelClientUnavailable,
    build_model_client,
)
from forge_agent.testing import ScriptedModelClient, finish_response
from forge_contracts import AgentObjective, AgentRunResult, ModelClient
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


def _scripted_client() -> ScriptedModelClient:
    """The offline-safe deterministic client used when no BYOK creds are present."""
    return ScriptedModelClient(
        responses=[],
        default=finish_response(
            "Objective acknowledged; no offline model actions were required.",
            confidence=0.9,
        ),
    )


def _default_redactor() -> Callable[[str], str]:
    """The shared secret redactor (``forge_api``), or identity if unavailable."""
    try:
        from forge_api.observability.redaction import redact_text
    except Exception:  # pragma: no cover - forge_api always present in the worker
        return lambda value: value
    return redact_text


def build_agent_runner(
    *,
    workspace_id: uuid.UUID | None = None,
    model_client: ModelClient | None = None,
) -> AgentRunner:
    """Build the agent runner, resolving a real BYOK ``ModelClient`` when possible.

    Resolution order:

    1. an explicitly injected ``model_client`` (tests / DI);
    2. a real provider client when ``FORGE_MODEL_PROVIDER`` + a BYOK key are in
       the environment (the integration lane) and the provider SDK is installed;
    3. otherwise the offline deterministic :class:`ScriptedModelClient`, so the
       worker still runs end-to-end (degraded, network-free).

    ``workspace_id`` is accepted for the per-workspace vault path (resolved via
    ``forge_api.auth.service.resolve_model_client``); the env path above is the
    integration-lane default and takes precedence when configured.
    """
    del workspace_id  # env-based resolution below; vault path lives in the API
    if model_client is not None:
        return AgentRunner(model_client)
    config = ModelClientConfig.from_env()
    if config is not None:
        try:
            client = build_model_client(config, redactor=_default_redactor())
        except ModelClientUnavailable:
            # Provider SDK/extra absent — never silently fake on a configured
            # lane failure; degrade to the offline client and keep running.
            pass
        else:
            return AgentRunner(client)
    return AgentRunner(_scripted_client())


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
