"""Celery beat schedule for the Forge worker (F19 sandbox reaper).

Registers the periodic ``sandbox.reap_orphans`` beat entry on the shared
``celery_app``. The cadence comes from ``FORGE_SANDBOX_REAP_INTERVAL_SECONDS``
(default 300s). Imported via the app ``include`` so the schedule is present
whenever the app is loaded for ``celery beat``.
"""

from __future__ import annotations

import os

from celery.schedules import crontab

from forge_worker.celery_app import celery_app

SANDBOX_REAP_TASK = "sandbox.reap_orphans"
MCP_REFRESH_TASK = "forge.knowledge.refresh_stale_mcp_sources"
AUTOMATION_SWEEP_TASK = "forge.automations.sweep_unprocessed_triggers"
# F26: daily burndown snapshot for every active sprint (workspace-naive UTC).
SPRINT_SNAPSHOT_TASK = "sprint.snapshot_burndown"


def reap_interval_seconds() -> float:
    return float(os.environ.get("FORGE_SANDBOX_REAP_INTERVAL_SECONDS", "300"))


def mcp_index_poll_seconds() -> float:
    """Beat cadence for the F20 stale-MCP-source refresh (``MCP_INDEX_POLL_SECONDS``)."""
    return float(os.environ.get("MCP_INDEX_POLL_SECONDS", "300"))


def automation_sweep_seconds() -> float:
    """Beat cadence for the F21 trigger reconciliation sweep."""
    return float(os.environ.get("FORGE_AUTOMATION_SWEEP_INTERVAL_SECONDS", "60"))


def configure_beat(app: object) -> dict[str, object]:
    """Install the periodic beat entries on ``app`` and return the schedule."""
    schedule = {
        "sandbox-reap-orphans": {
            "task": SANDBOX_REAP_TASK,
            "schedule": reap_interval_seconds(),
        },
        # F20: periodically refresh stale sync-and-index MCP sources.
        "mcp-refresh-stale-sources": {
            "task": MCP_REFRESH_TASK,
            "schedule": mcp_index_poll_seconds(),
        },
        # F21: reconcile any trigger envelopes whose enqueue was lost.
        "automation-sweep-unprocessed-triggers": {
            "task": AUTOMATION_SWEEP_TASK,
            "schedule": automation_sweep_seconds(),
        },
        # F26: snapshot every active sprint's burndown daily at 23:55 UTC.
        "sprint-snapshot-burndown": {
            "task": SPRINT_SNAPSHOT_TASK,
            "schedule": crontab(hour=23, minute=55),
        },
    }
    existing = dict(getattr(app.conf, "beat_schedule", {}) or {})  # type: ignore[attr-defined]
    existing.update(schedule)
    app.conf.beat_schedule = existing  # type: ignore[attr-defined]
    return existing


BEAT_SCHEDULE = configure_beat(celery_app)

__all__ = [
    "AUTOMATION_SWEEP_TASK",
    "BEAT_SCHEDULE",
    "MCP_REFRESH_TASK",
    "SANDBOX_REAP_TASK",
    "SPRINT_SNAPSHOT_TASK",
    "automation_sweep_seconds",
    "configure_beat",
    "mcp_index_poll_seconds",
    "reap_interval_seconds",
]
