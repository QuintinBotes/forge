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
# F30: purge expired role grants every 5m (hygiene + audit; expiry is
# authoritative at resolution time, so a missed run never grants stale access).
AUTHZ_PURGE_TASK = "authz.purge_expired_grants"
# F37: revoke expired platform API keys every 15m (hygiene + audit; expiry is
# authoritative at verify time — Security "automatic expiry for agent tokens").
AUTH_PURGE_KEYS_TASK = "auth.purge_expired_keys"
# F32: hourly marketplace catalog sync + update-flag refresh across all workspaces.
MARKETPLACE_SYNC_TASK = "marketplace.sync_all_registries"
MARKETPLACE_REFRESH_TASK = "marketplace.refresh_update_flags"
# F33: 6-hourly SAML IdP metadata refresh (cert rollover) + 15-minute eviction
# of expired saml_replay rows (the no-Redis fallback replay store).
SSO_METADATA_REFRESH_TASK = "sso.refresh_all_saml_metadata"
SSO_REPLAY_CLEANUP_TASK = "sso.cleanup_saml_replay"
# F36: sweep pending approval gates past their expires_at SLA (default 60s).
APPROVAL_EXPIRE_TASK = "approvals.expire_pending"


def reap_interval_seconds() -> float:
    return float(os.environ.get("FORGE_SANDBOX_REAP_INTERVAL_SECONDS", "300"))


def marketplace_sync_seconds() -> float:
    """Beat cadence for the F32 catalog sync (``MARKETPLACE_SYNC_INTERVAL_MINUTES``)."""
    return float(os.environ.get("MARKETPLACE_SYNC_INTERVAL_MINUTES", "60")) * 60.0


def authz_purge_seconds() -> float:
    """Beat cadence for the F30 expired-grant purge (``FORGE_AUTHZ_PURGE_INTERVAL_SECONDS``)."""
    return float(os.environ.get("FORGE_AUTHZ_PURGE_INTERVAL_SECONDS", "300"))


def auth_purge_keys_seconds() -> float:
    """Beat cadence for the F37 expired-platform-key purge (default 15m)."""
    return float(os.environ.get("FORGE_AUTH_PURGE_KEYS_INTERVAL_SECONDS", "900"))


def mcp_index_poll_seconds() -> float:
    """Beat cadence for the F20 stale-MCP-source refresh (``MCP_INDEX_POLL_SECONDS``)."""
    return float(os.environ.get("MCP_INDEX_POLL_SECONDS", "300"))


def sso_metadata_refresh_seconds() -> float:
    """Beat cadence for the F33 IdP metadata refresh (default 6h)."""
    return float(os.environ.get("FORGE_SSO_METADATA_REFRESH_SECONDS", str(6 * 3600)))


def sso_replay_cleanup_seconds() -> float:
    """Beat cadence for the F33 saml_replay eviction (default 15m)."""
    return float(os.environ.get("FORGE_SSO_REPLAY_CLEANUP_SECONDS", "900"))


def automation_sweep_seconds() -> float:
    """Beat cadence for the F21 trigger reconciliation sweep."""
    return float(os.environ.get("FORGE_AUTOMATION_SWEEP_INTERVAL_SECONDS", "60"))


def approval_expire_seconds() -> float:
    """Beat cadence for the F36 approval-SLA sweep (default 60s)."""
    return float(os.environ.get("FORGE_APPROVAL_EXPIRE_INTERVAL_SECONDS", "60"))


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
        # F30: purge expired role grants on a fixed cadence (default 5m).
        "authz-purge-expired-grants": {
            "task": AUTHZ_PURGE_TASK,
            "schedule": authz_purge_seconds(),
        },
        # F37: revoke expired platform API keys (agent tokens) on a fixed cadence.
        "auth-purge-expired-keys": {
            "task": AUTH_PURGE_KEYS_TASK,
            "schedule": auth_purge_keys_seconds(),
        },
        # F32: hourly marketplace catalog sync, then recompute update/yank flags.
        "marketplace-sync-all-registries": {
            "task": MARKETPLACE_SYNC_TASK,
            "schedule": marketplace_sync_seconds(),
        },
        "marketplace-refresh-update-flags": {
            "task": MARKETPLACE_REFRESH_TASK,
            "schedule": marketplace_sync_seconds(),
        },
        # F33: refresh SAML IdP metadata (cert rollover) + evict expired replay ids.
        "sso-refresh-saml-metadata": {
            "task": SSO_METADATA_REFRESH_TASK,
            "schedule": sso_metadata_refresh_seconds(),
        },
        "sso-cleanup-saml-replay": {
            "task": SSO_REPLAY_CLEANUP_TASK,
            "schedule": sso_replay_cleanup_seconds(),
        },
        # F36: mark pending approval gates past expires_at as expired.
        "approvals-expire-pending": {
            "task": APPROVAL_EXPIRE_TASK,
            "schedule": approval_expire_seconds(),
        },
    }
    existing = dict(getattr(app.conf, "beat_schedule", {}) or {})  # type: ignore[attr-defined]
    existing.update(schedule)
    app.conf.beat_schedule = existing  # type: ignore[attr-defined]
    return existing


BEAT_SCHEDULE = configure_beat(celery_app)

__all__ = [
    "APPROVAL_EXPIRE_TASK",
    "AUTHZ_PURGE_TASK",
    "AUTH_PURGE_KEYS_TASK",
    "AUTOMATION_SWEEP_TASK",
    "BEAT_SCHEDULE",
    "MARKETPLACE_REFRESH_TASK",
    "MARKETPLACE_SYNC_TASK",
    "MCP_REFRESH_TASK",
    "SANDBOX_REAP_TASK",
    "SPRINT_SNAPSHOT_TASK",
    "SSO_METADATA_REFRESH_TASK",
    "SSO_REPLAY_CLEANUP_TASK",
    "approval_expire_seconds",
    "auth_purge_keys_seconds",
    "authz_purge_seconds",
    "automation_sweep_seconds",
    "configure_beat",
    "marketplace_sync_seconds",
    "mcp_index_poll_seconds",
    "reap_interval_seconds",
    "sso_metadata_refresh_seconds",
    "sso_replay_cleanup_seconds",
]
