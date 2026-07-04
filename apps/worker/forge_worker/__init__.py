"""Celery worker application: indexer, syncer, and agent-runner tasks.

Exposes the Celery application as ``app`` (and ``celery_app``) at the package
root so ``celery -A forge_worker worker`` resolves it (the compose ``worker``
service entrypoint).
"""

from __future__ import annotations

from forge_worker.celery_app import celery_app
from forge_worker.celery_app import celery_app as app

__version__ = "0.1.0"

__all__ = ["app", "celery_app"]
