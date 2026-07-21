"""Celery application for the Forge worker (indexer, syncer, agent-runner).

The :class:`~celery.Celery` instance is created at import time but does **not**
open a broker connection until a worker starts or a task is dispatched, so
importing this module stays hermetic (tests register/inspect tasks without a live
Redis). Broker/result-backend URLs come from ``FORGE_REDIS_URL`` to match the
rest of the workspace; ``include`` pre-imports the task modules so their
``@celery_app.task`` registrations are present.
"""

from __future__ import annotations

import os

from celery import Celery

DEFAULT_REDIS_URL = "redis://localhost:6379/0"


def get_broker_url() -> str:
    """Resolve the Celery broker/result URL from the environment."""
    return os.environ.get("FORGE_REDIS_URL", DEFAULT_REDIS_URL)


celery_app = Celery(
    "forge",
    broker=get_broker_url(),
    backend=get_broker_url(),
    include=[
        "forge_worker.indexer",
        "forge_worker.syncer",
        "forge_worker.agent_runner",
        "forge_worker.tasks.incident",
        "forge_worker.tasks.sandbox",
        "forge_worker.tasks.knowledge_mcp",
        "forge_worker.tasks.automations",
        "forge_worker.tasks.sprint_tasks",
        "forge_worker.tasks.auth",
        "forge_worker.tasks.authz",
        "forge_worker.tasks.approvals",
        "forge_worker.tasks.marketplace",
        "forge_worker.tasks.observability",
        "forge_worker.tasks.sso",
        "forge_worker.tasks.audit",
        "forge_worker.tasks.self_eval_mint",
        "forge_worker.tasks.self_eval_run",
        "forge_worker.tasks.deployments",
        "forge_worker.beat",
    ],
)
celery_app.conf.task_default_queue = "forge"

# HARD-11: apply the env-driven reliability knobs (acks_late + reject-on-lost so
# an interrupted task re-queues, fair prefetch, per-child task cap bounding
# memory, soft/hard time limits for runaway tasks). Default-on with safe
# defaults so dev/test are unaffected; overridden via FORGE_* env vars.
from forge_worker.reliability import configure_reliability  # noqa: E402

configure_reliability(celery_app)


# F38 + HARD-10: one shared telemetry init per worker process (env-driven; the
# lean default installs no-op providers and never opens a connection, while
# OBS_ENABLED=true + an OTLP endpoint stands up the real exporters). Delegates
# to the worker observability module (which also owns the process UsageMeter).
def _init_worker_telemetry(**_kwargs: object) -> None:  # pragma: no cover - worker boot
    from forge_worker.observability import setup_worker_telemetry

    setup_worker_telemetry()


try:  # connecting the signal is safe at import; it fires only in real workers
    from celery.signals import worker_process_init

    worker_process_init.connect(_init_worker_telemetry, weak=False)
except Exception:  # pragma: no cover - celery signal API unavailable
    pass

__all__ = ["celery_app", "get_broker_url"]
