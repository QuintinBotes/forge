"""Asana webhook verification + parsing.

Asana signs webhook deliveries with an HMAC-SHA256 digest of the raw body,
keyed on the secret established during the handshake (Asana echoes an
``X-Hook-Secret`` header back at registration time; the API layer is
responsible for persisting the echoed value as the connection's webhook
secret — this module only verifies against it). The
``X-Hook-Signature`` header carries the hex digest.

A single delivery can carry multiple ``events``; F40 treats the payload as a
*hint* only (state is always re-fetched), so :func:`parse_asana` normalizes the
**first** event to the shared :class:`WebhookEvent` shape.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Any

from forge_contracts.pm import PMProvider, WebhookEvent

SIGNATURE_HEADER = "x-hook-signature"

# Asana event "action" -> normalized F18/F40 event_type.
_ACTION_MAP: dict[str, str] = {
    "added": "issue.created",
    "changed": "issue.updated",
    "removed": "issue.updated",
    "deleted": "issue.deleted",
    "undeleted": "issue.updated",
}


def sign_asana(secret: str, body: bytes) -> str:
    """Compute the ``X-Hook-Signature`` hex digest for ``body``."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_asana(secret: str, body: bytes, signature: str | None) -> bool:
    """Constant-time verify the HMAC-SHA256 signature over the raw body."""
    if not secret or not signature:
        return False
    expected = sign_asana(secret, body)
    return hmac.compare_digest(expected, signature)


def synthesize_delivery_id(body: bytes, *, received_at: datetime | None = None) -> str:
    """Asana supplies no stable delivery id; synthesize ``sha256(body)+minute``."""
    now = received_at or datetime.now(UTC)
    minute = now.strftime("%Y%m%d%H%M")
    return f"{hashlib.sha256(body).hexdigest()}:{minute}"


def parse_asana(body: dict[str, Any], *, delivery_id: str, signature_valid: bool) -> WebhookEvent:
    """Map the first Asana webhook event onto the normalized :class:`WebhookEvent`."""
    events = body.get("events") or []
    first = events[0] if events else {}
    action = str(first.get("action") or "").lower()
    event_type = _ACTION_MAP.get(action, "issue.updated")
    resource = first.get("resource") or {}
    external_id = resource.get("gid")
    return WebhookEvent(
        provider=PMProvider.asana,
        delivery_id=delivery_id,
        event_type=event_type,  # type: ignore[arg-type]
        external_id=str(external_id) if external_id is not None else None,
        external_key=str(external_id) if external_id is not None else None,
        signature_valid=signature_valid,
        received_at=datetime.now(UTC),
        payload={"action": action, "resource_gid": external_id},
    )


__all__ = [
    "SIGNATURE_HEADER",
    "parse_asana",
    "sign_asana",
    "synthesize_delivery_id",
    "verify_asana",
]
