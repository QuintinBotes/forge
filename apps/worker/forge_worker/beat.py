"""Celery beat schedule for the Forge worker (F19 sandbox reaper).

Registers the periodic ``sandbox.reap_orphans`` beat entry on the shared
``celery_app``. The cadence comes from ``FORGE_SANDBOX_REAP_INTERVAL_SECONDS``
(default 300s). Imported via the app ``include`` so the schedule is present
whenever the app is loaded for ``celery beat``.
"""

from __future__ import annotations

import os

from forge_worker.celery_app import celery_app

SANDBOX_REAP_TASK = "sandbox.reap_orphans"


def reap_interval_seconds() -> float:
    return float(os.environ.get("FORGE_SANDBOX_REAP_INTERVAL_SECONDS", "300"))


def configure_beat(app: object) -> dict[str, object]:
    """Install the sandbox-reaper beat entry on ``app`` and return the schedule."""
    schedule = {
        "sandbox-reap-orphans": {
            "task": SANDBOX_REAP_TASK,
            "schedule": reap_interval_seconds(),
        },
    }
    existing = dict(getattr(app.conf, "beat_schedule", {}) or {})  # type: ignore[attr-defined]
    existing.update(schedule)
    app.conf.beat_schedule = existing  # type: ignore[attr-defined]
    return existing


BEAT_SCHEDULE = configure_beat(celery_app)

__all__ = ["BEAT_SCHEDULE", "SANDBOX_REAP_TASK", "configure_beat", "reap_interval_seconds"]
