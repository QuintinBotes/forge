"""Linear webhook verification (HMAC-SHA256 over raw body) + parsing.

Linear signs the **exact raw request body** with the per-webhook secret
(``Linear-Signature`` header) and includes a ``webhookTimestamp`` that must be
fresh. We verify with a constant-time compare and reject stale timestamps to
defeat replay.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Any

from forge_contracts.pm import PMProvider, WebhookEvent

# Linear webhook action -> normalized F18 event_type.
_ACTION_MAP: dict[str, str] = {
    "create": "issue.created",
    "update": "issue.updated",
    "remove": "issue.deleted",
}

DEFAULT_TOLERANCE_SECONDS = 60


def sign_linear(secret: str, body: bytes) -> str:
    """Compute the Linear-Signature hex digest for ``body``."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_linear(
    secret: str,
    body: bytes,
    signature: str | None,
    *,
    timestamp_ms: int | None = None,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    now: float | None = None,
) -> bool:
    """Verify the HMAC-SHA256 signature over the raw body + timestamp freshness."""
    if not secret or not signature:
        return False
    expected = sign_linear(secret, body)
    if not hmac.compare_digest(expected, signature):
        return False
    if timestamp_ms is not None:
        current = now if now is not None else datetime.now(UTC).timestamp()
        if abs(current - timestamp_ms / 1000) > tolerance_seconds:
            return False
    return True


def parse_linear(body: dict[str, Any], *, delivery_id: str, signature_valid: bool) -> WebhookEvent:
    """Map a Linear webhook body onto the normalized :class:`WebhookEvent`."""
    action = str(body.get("action") or "").lower()
    event_type = _ACTION_MAP.get(action, "issue.updated")
    data = body.get("data") or {}
    external_id = data.get("id")
    external_key = data.get("identifier")
    ts = body.get("webhookTimestamp")
    received_at = (
        datetime.fromtimestamp(ts / 1000, tz=UTC)
        if isinstance(ts, (int, float))
        else datetime.now(UTC)
    )
    return WebhookEvent(
        provider=PMProvider.linear,
        delivery_id=delivery_id,
        event_type=event_type,  # type: ignore[arg-type]
        external_id=str(external_id) if external_id is not None else None,
        external_key=external_key,
        signature_valid=signature_valid,
        received_at=received_at,
        payload={"action": action, "type": body.get("type"), "identifier": external_key},
    )


__all__ = ["DEFAULT_TOLERANCE_SECONDS", "parse_linear", "sign_linear", "verify_linear"]
