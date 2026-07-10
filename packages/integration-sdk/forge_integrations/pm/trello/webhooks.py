"""Trello webhook verification + parsing.

Trello signs webhook deliveries with ``base64(HMAC-SHA1(secret, body +
callback_url))`` — the callback URL is part of the signed material, so
verification needs it alongside the secret (see Trello's webhook docs). The
adapter persists the callback URL used at registration time onto
``AdapterContext.config["webhook_callback_url"]`` (mirroring monday.com's
precedent of persisting the echoed per-connection secret) so
``verify_webhook``'s fixed ``(body, headers, secret)`` Protocol signature can
still be satisfied. The digest is carried in the ``X-Trello-Webhook`` header.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import UTC, datetime
from typing import Any

from forge_contracts.pm import PMProvider, WebhookEvent

SIGNATURE_HEADER = "x-trello-webhook"

# Trello webhook "action.type" -> normalized F18/F40 event_type.
_ACTION_MAP: dict[str, str] = {
    "createCard": "issue.created",
    "updateCard": "issue.updated",
    "deleteCard": "issue.deleted",
}


def sign_trello(secret: str, body: bytes, callback_url: str) -> str:
    """Compute the ``X-Trello-Webhook`` base64 digest for ``body`` + ``callback_url``."""
    digest = hmac.new(secret.encode(), body + callback_url.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def verify_trello(secret: str, callback_url: str, body: bytes, signature: str | None) -> bool:
    """Constant-time verify the HMAC-SHA1 signature over ``body`` + ``callback_url``."""
    if not secret or not signature or not callback_url:
        return False
    return hmac.compare_digest(sign_trello(secret, body, callback_url), signature)


def synthesize_delivery_id(body: bytes, *, received_at: datetime | None = None) -> str:
    """Trello supplies no stable delivery id; synthesize ``sha256(body)+minute``."""
    now = received_at or datetime.now(UTC)
    minute = now.strftime("%Y%m%d%H%M")
    return f"{hashlib.sha256(body).hexdigest()}:{minute}"


def parse_trello(body: dict[str, Any], *, delivery_id: str, signature_valid: bool) -> WebhookEvent:
    """Map a Trello webhook body onto the normalized :class:`WebhookEvent`."""
    action = body.get("action") or {}
    action_type = str(action.get("type") or "")
    event_type = _ACTION_MAP.get(action_type, "issue.updated")
    card = (action.get("data") or {}).get("card") or {}
    external_id = card.get("id")
    return WebhookEvent(
        provider=PMProvider.trello,
        delivery_id=delivery_id,
        event_type=event_type,  # type: ignore[arg-type]
        external_id=str(external_id) if external_id is not None else None,
        external_key=str(external_id) if external_id is not None else None,
        signature_valid=signature_valid,
        received_at=datetime.now(UTC),
        payload={"type": action_type},
    )


__all__ = [
    "SIGNATURE_HEADER",
    "parse_trello",
    "sign_trello",
    "synthesize_delivery_id",
    "verify_trello",
]
