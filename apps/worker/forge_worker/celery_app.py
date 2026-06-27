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
        "forge_worker.beat",
    ],
)
celery_app.conf.task_default_queue = "forge"

__all__ = ["celery_app", "get_broker_url"]
