"""Jira webhook verification + parsing.

Atlassian does not HMAC-sign Jira REST webhooks, so F18 binds a per-connection
random secret into the registered webhook (path/header) and verifies it with a
constant-time compare. The payload is always treated as a *hint*: the worker
re-fetches authoritative state before any board write.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Any

from forge_contracts.pm import PMProvider, WebhookEvent

# Jira webhookEvent -> normalized F18 event_type.
_EVENT_MAP: dict[str, str] = {
    "jira:issue_created": "issue.created",
    "issue_created": "issue.created",
    "jira:issue_updated": "issue.updated",
    "issue_updated": "issue.updated",
    "jira:issue_deleted": "issue.deleted",
    "issue_deleted": "issue.deleted",
}


def verify_jira(secret: str, provided: str | None) -> bool:
    """Constant-time compare the per-connection webhook secret."""
    if not secret or not provided:
        return False
    return hmac.compare_digest(secret, provided)


def synthesize_delivery_id(body: bytes, *, received_at: datetime | None = None) -> str:
    """Jira supplies no delivery id; synthesize ``sha256(body)+received_minute``."""
    now = received_at or datetime.now(UTC)
    minute = now.strftime("%Y%m%d%H%M")
    return f"{hashlib.sha256(body).hexdigest()}:{minute}"


def parse_jira(body: dict[str, Any], *, delivery_id: str, signature_valid: bool) -> WebhookEvent:
    """Map a Jira webhook body onto the normalized :class:`WebhookEvent`."""
    raw_event = str(body.get("webhookEvent") or body.get("issue_event_type_name") or "")
    event_type = _EVENT_MAP.get(raw_event, "issue.updated")
    issue = body.get("issue") or {}
    external_id = str(issue.get("id")) if issue.get("id") is not None else None
    external_key = issue.get("key")
    ts = body.get("timestamp")
    received_at = (
        datetime.fromtimestamp(ts / 1000, tz=UTC)
        if isinstance(ts, (int, float))
        else datetime.now(UTC)
    )
    return WebhookEvent(
        provider=PMProvider.jira,
        delivery_id=delivery_id,
        event_type=event_type,  # type: ignore[arg-type]
        external_id=external_id,
        external_key=external_key,
        signature_valid=signature_valid,
        received_at=received_at,
        payload={"webhookEvent": raw_event, "issue_key": external_key},
    )


__all__ = ["parse_jira", "synthesize_delivery_id", "verify_jira"]
