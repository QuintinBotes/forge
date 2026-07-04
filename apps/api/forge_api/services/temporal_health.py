"""Temporal reachability for ``/readyz`` (F25 AC19).

Adds a ``temporal`` readiness check **only** when the Temporal backend is
selected, so F14's readiness contract stays honest: with the FSM backend the
endpoint never touches Temporal; with the Temporal backend it reports 503 (listing
``temporal``) while the frontend is unreachable and 200 once healthy.
"""

from __future__ import annotations

import asyncio

from forge_workflow.temporal.config import TemporalSettings

#: Seconds to wait for a Temporal frontend connection before declaring it down.
_CONNECT_TIMEOUT = 2.0


async def _acheck(settings: TemporalSettings) -> bool:
    from forge_workflow.temporal.client import get_temporal_client

    client = await asyncio.wait_for(get_temporal_client(settings), timeout=_CONNECT_TIMEOUT)
    await asyncio.wait_for(client.service_client.check_health(), timeout=_CONNECT_TIMEOUT)
    return True


def temporal_reachable(settings: TemporalSettings | None = None) -> bool:
    """True if the Temporal frontend connects + reports healthy; else False."""
    settings = settings or TemporalSettings()
    try:
        return asyncio.run(_acheck(settings))
    except Exception:
        return False


def readiness(settings: TemporalSettings | None = None) -> tuple[bool, dict[str, str]]:
    """Return ``(ready, checks)`` augmenting readiness with Temporal when selected."""
    settings = settings or TemporalSettings()
    checks = {"process": "ok"}
    if settings.workflow_engine_backend != "temporal":
        return True, checks
    if temporal_reachable(settings):
        checks["temporal"] = "ok"
        return True, checks
    checks["temporal"] = "unreachable"
    return False, checks


__all__ = ["readiness", "temporal_reachable"]
