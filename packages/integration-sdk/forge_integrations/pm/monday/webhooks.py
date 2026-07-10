"""Monday.com webhook verification + parsing.

monday.com does not HMAC-sign webhook deliveries; instead a per-webhook custom
header (we register one carrying a random per-connection secret — see
``MondayAdapter.register_webhook``) is echoed back on every event POST, and
verified here with a constant-time compare (same model as Jira's per-connection
secret header, F18 §16).

monday.com also performs a *handshake* the first time a webhook URL is
registered: it POSTs ``{"challenge": "<token>"}`` and expects the exact same
JSON echoed back (before any signature exists to check). :func:`is_challenge`
/ :func:`challenge_response` let the API layer's webhook route handle that
one-time exchange; it is orthogonal to the steady-state
``parse_webhook``/``verify_webhook`` pair below.
"""

from __future__ import annotations

import hmac
from datetime import UTC, datetime
from typing import Any

from forge_contracts.pm import PMProvider, WebhookEvent

SECRET_HEADER = "x-forge-pm-secret"

# monday.com event "type" -> normalized F18/F40 event_type.
_EVENT_MAP: dict[str, str] = {
    "create_pulse": "issue.created",
    "create_item": "issue.created",
    "delete_pulse": "issue.deleted",
    "delete_item": "issue.deleted",
}


def is_challenge(body: dict[str, Any]) -> bool:
    """True when ``body`` is monday's one-time webhook-registration handshake."""
    return "challenge" in body


def challenge_response(body: dict[str, Any]) -> dict[str, Any]:
    """The exact JSON monday.com expects echoed back to confirm a webhook URL."""
    return {"challenge": body.get("challenge")}


def verify_monday(secret: str, provided: str | None) -> bool:
    """Constant-time compare the per-connection webhook secret header."""
    if not secret or not provided:
        return False
    return hmac.compare_digest(secret, provided)


def parse_monday(body: dict[str, Any], *, delivery_id: str, signature_valid: bool) -> WebhookEvent:
    """Map a monday.com webhook body onto the normalized :class:`WebhookEvent`."""
    event = body.get("event") or {}
    event_type = _EVENT_MAP.get(str(event.get("type") or ""), "issue.updated")
    pulse_id = event.get("pulseId")
    external_id = str(pulse_id) if pulse_id is not None else None
    changed_at = event.get("changedAt") or event.get("triggerTime")
    received_at = _parse_ts(changed_at)
    return WebhookEvent(
        provider=PMProvider.monday,
        delivery_id=delivery_id,
        event_type=event_type,  # type: ignore[arg-type]
        external_id=external_id,
        external_key=external_id,
        signature_valid=signature_valid,
        received_at=received_at,
        payload={"type": event.get("type"), "board_id": event.get("boardId")},
    )


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(UTC)


__all__ = [
    "SECRET_HEADER",
    "challenge_response",
    "is_challenge",
    "parse_monday",
    "verify_monday",
]
