"""ClickUp webhook verification + parsing.

ClickUp signs webhook deliveries with an HMAC-SHA256 hex digest of the raw
body, keyed on the secret returned at webhook-registration time. The digest
is carried in the ``X-Signature`` header.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Any

from forge_contracts.pm import PMProvider, WebhookEvent

SIGNATURE_HEADER = "x-signature"

# ClickUp webhook "event" -> normalized F18/F40 event_type.
_EVENT_MAP: dict[str, str] = {
    "taskCreated": "issue.created",
    "taskUpdated": "issue.updated",
    "taskStatusUpdated": "issue.updated",
    "taskPriorityUpdated": "issue.updated",
    "taskDeleted": "issue.deleted",
}


def sign_clickup(secret: str, body: bytes) -> str:
    """Compute the ``X-Signature`` hex digest for ``body``."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_clickup(secret: str, body: bytes, signature: str | None) -> bool:
    """Constant-time verify the HMAC-SHA256 signature over the raw body."""
    if not secret or not signature:
        return False
    return hmac.compare_digest(sign_clickup(secret, body), signature)


def synthesize_delivery_id(body: bytes, *, received_at: datetime | None = None) -> str:
    """ClickUp supplies no stable delivery id; synthesize ``sha256(body)+minute``."""
    now = received_at or datetime.now(UTC)
    minute = now.strftime("%Y%m%d%H%M")
    return f"{hashlib.sha256(body).hexdigest()}:{minute}"


def parse_clickup(body: dict[str, Any], *, delivery_id: str, signature_valid: bool) -> WebhookEvent:
    """Map a ClickUp webhook body onto the normalized :class:`WebhookEvent`."""
    event = str(body.get("event") or "")
    event_type = _EVENT_MAP.get(event, "issue.updated")
    history_items = body.get("history_items") or []
    task_id = body.get("task_id")
    if not task_id and history_items:
        task_id = (history_items[0] or {}).get("parent_id")
    return WebhookEvent(
        provider=PMProvider.clickup,
        delivery_id=delivery_id,
        event_type=event_type,  # type: ignore[arg-type]
        external_id=str(task_id) if task_id is not None else None,
        external_key=str(task_id) if task_id is not None else None,
        signature_valid=signature_valid,
        received_at=datetime.now(UTC),
        payload={"event": event},
    )


__all__ = [
    "SIGNATURE_HEADER",
    "parse_clickup",
    "sign_clickup",
    "synthesize_delivery_id",
    "verify_clickup",
]
