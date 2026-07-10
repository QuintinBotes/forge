"""GitLab issue-hook verification + parsing.

GitLab does not HMAC-sign webhook deliveries; instead a per-hook secret token
is echoed back verbatim on every event POST in the ``X-Gitlab-Token`` header,
verified here with a constant-time compare (same model as Jira's/monday.com's
per-connection secret header). Newer GitLab versions additionally send a
stable ``X-Gitlab-Event-UUID`` delivery id, used when present.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Any

from forge_contracts.pm import PMProvider, WebhookEvent

SECRET_HEADER = "x-gitlab-token"
DELIVERY_ID_HEADER = "x-gitlab-event-uuid"

# GitLab issue-hook "object_attributes.action" -> normalized F18/F40 event_type.
_ACTION_MAP: dict[str, str] = {
    "open": "issue.created",
    "reopen": "issue.updated",
    "update": "issue.updated",
    "close": "issue.updated",
    "delete": "issue.deleted",
}


def verify_gitlab(secret: str, provided: str | None) -> bool:
    """Constant-time compare the per-hook secret token header."""
    if not secret or not provided:
        return False
    return hmac.compare_digest(secret, provided)


def synthesize_delivery_id(body: bytes) -> str:
    """Fallback delivery id when ``X-Gitlab-Event-UUID`` is absent."""
    return hashlib.sha256(body).hexdigest()


def parse_gitlab(body: dict[str, Any], *, delivery_id: str, signature_valid: bool) -> WebhookEvent:
    """Map a GitLab ``issue`` hook body onto the normalized :class:`WebhookEvent`."""
    attrs = body.get("object_attributes") or {}
    action = str(attrs.get("action") or "")
    event_type = _ACTION_MAP.get(action, "issue.updated")
    external_id = attrs.get("iid")
    return WebhookEvent(
        provider=PMProvider.gitlab,
        delivery_id=delivery_id,
        event_type=event_type,  # type: ignore[arg-type]
        external_id=str(external_id) if external_id is not None else None,
        external_key=str(external_id) if external_id is not None else None,
        signature_valid=signature_valid,
        received_at=datetime.now(UTC),
        payload={"action": action, "object_kind": body.get("object_kind")},
    )


__all__ = [
    "DELIVERY_ID_HEADER",
    "SECRET_HEADER",
    "parse_gitlab",
    "synthesize_delivery_id",
    "verify_gitlab",
]
