"""Approval events on ``/ws`` (spec L seam 4, §6a).

One builder for every path that opens or changes a gate (the approvals router,
the V1 engine, the Temporal worker, the SLA sweep), so their events cannot
drift. ``approval.requested`` when a gate opens, ``approval.decided`` for a final
status, ``approval.updated`` while it stays pending (escalated, partially
approved, routed). Payloads carry identifiers, gate, title, risk and expiry,
never ``gate_payload``: clients read ``GET /approvals/{id}/context``.

Processes without the API's event loop publish with :func:`publish_sync` onto
the Redis channel the API relays (:class:`RedisBroadcaster.run_forever`).
"""

from __future__ import annotations

import logging
from typing import Any

import anyio.to_thread

from forge_api.realtime.broadcaster import _CHANNEL_PREFIX, realtime_redis_enabled
from forge_approval import ENFORCED_GATES, ApprovalService
from forge_approval.models import ApprovalRequest, GateStatus
from forge_contracts import RealtimeEvent, RealtimeEventType

logger = logging.getLogger(__name__)

_FIELDS = {
    "title",
    "risk_level",
    "project_id",
    "task_id",
    "workflow_run_id",
    "requested_actor",
    "expires_at",
    "required_approvals",
}


def _event(type_: RealtimeEventType, request: ApprovalRequest, **extra: Any) -> RealtimeEvent:
    payload = {
        "gate": request.gate_type.value,
        **request.model_dump(mode="json", include=_FIELDS),
        **extra,
    }
    return RealtimeEvent(
        type=type_,
        workspace_id=request.workspace_id,
        approval_id=request.id,
        payload=payload,
        enforceable=request.gate_type in ENFORCED_GATES,
    )


def requested(request: ApprovalRequest) -> RealtimeEvent:
    """A gate opened."""
    return _event(RealtimeEventType.APPROVAL_REQUESTED, request)


def decided(request: ApprovalRequest, *, decided_by: str, surface: str | None) -> RealtimeEvent:
    """A gate reached a final status; ``surface`` comes from the decider's credential."""
    return _event(
        RealtimeEventType.APPROVAL_DECIDED,
        request,
        status=request.status.value,
        decided_by=decided_by,
        surface=surface,
    )


def updated(request: ApprovalRequest, *, reason: str) -> RealtimeEvent:
    """A gate changed but is still pending (clients re-fetch it)."""
    return _event(
        RealtimeEventType.APPROVAL_UPDATED, request, status=GateStatus.PENDING.value, reason=reason
    )


def relay_url() -> str | None:
    """The Redis URL the API relays ``/ws`` events from; ``None`` on the in-process backend."""
    from forge_api.settings import get_settings

    settings = get_settings()
    return settings.redis_url if realtime_redis_enabled(settings) else None


def publish_sync(redis_url: str | None, event: RealtimeEvent) -> None:
    """Publish ``event`` where the API relays it, best effort (never raises).

    Same channel and JSON as :class:`RedisBroadcaster.publish`. ``None`` (the
    in-process backend, which no other process can reach) publishes nothing.
    """
    if redis_url is None:
        return
    # shortcut: one connection per event; reuse a client if a sweep ever sends many.
    try:
        import redis

        client = redis.Redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
        try:
            client.publish(f"{_CHANNEL_PREFIX}{event.workspace_id}", event.model_dump_json())
        finally:
            client.close()
    except Exception:
        logger.warning(
            "%s for approval %s was not published",
            event.type.value,
            event.approval_id,
            exc_info=True,
        )


async def create_published(
    service: ApprovalService, *, redis_url: str | None, **fields: Any
) -> ApprovalRequest:
    """``ApprovalService.create`` that announces the gate, for processes off the API loop.

    The worker's ``forge.create_approval`` uses it, and station gates (spec K P2
    wave 2) reuse it. ``create`` is idempotent while pending, so a retry
    announces the same gate again; clients apply events by id.
    """
    request = await service.create(**fields)
    # publish_sync blocks on Redis; keep it off the caller's event loop.
    await anyio.to_thread.run_sync(publish_sync, redis_url, requested(request))
    return request


__all__ = ["create_published", "decided", "publish_sync", "relay_url", "requested", "updated"]
