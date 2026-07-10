"""GitHub Projects v2 webhook verification + parsing.

GitHub signs **every** webhook delivery (including ``projects_v2_item``) with
the same ``X-Hub-Signature-256`` HMAC-SHA256 scheme, already implemented for
CI webhooks in :mod:`forge_integrations.webhooks`. This module reuses that
verifier verbatim instead of re-implementing GitHub's signing algorithm, and
only adds the ``projects_v2_item`` -> normalized :class:`WebhookEvent` mapping.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from forge_contracts.pm import PMProvider, WebhookEvent
from forge_integrations.webhooks import sign_github_payload, verify_github_signature

DELIVERY_ID_HEADER = "x-github-delivery"
SIGNATURE_HEADER = "x-hub-signature-256"

# GitHub `projects_v2_item` webhook "action" -> normalized F18/F40 event_type.
_ACTION_MAP: dict[str, str] = {
    "created": "issue.created",
    "edited": "issue.updated",
    "reordered": "issue.updated",
    "converted": "issue.updated",
    "archived": "issue.updated",
    "restored": "issue.updated",
    "deleted": "issue.deleted",
}


def verify_github_projects(secret: str, body: bytes, signature: str | None) -> bool:
    """Verify the ``X-Hub-Signature-256`` header (delegates to the shared helper)."""
    return verify_github_signature(secret, body, signature)


def parse_github_projects(
    body: dict[str, Any], *, delivery_id: str, signature_valid: bool
) -> WebhookEvent:
    """Map a ``projects_v2_item`` webhook body onto the normalized :class:`WebhookEvent`."""
    action = str(body.get("action") or "").lower()
    event_type = _ACTION_MAP.get(action, "issue.updated")
    item = body.get("projects_v2_item") or {}
    external_id = item.get("node_id")
    return WebhookEvent(
        provider=PMProvider.github_projects,
        delivery_id=delivery_id,
        event_type=event_type,  # type: ignore[arg-type]
        external_id=str(external_id) if external_id is not None else None,
        external_key=str(external_id) if external_id is not None else None,
        signature_valid=signature_valid,
        received_at=datetime.now(UTC),
        payload={"action": action, "content_type": item.get("content_type")},
    )


def synthesize_delivery_id(body: bytes) -> str:
    """Fallback delivery id when the ``X-GitHub-Delivery`` header is absent."""
    return hashlib.sha256(body).hexdigest()


__all__ = [
    "DELIVERY_ID_HEADER",
    "SIGNATURE_HEADER",
    "parse_github_projects",
    "sign_github_payload",
    "synthesize_delivery_id",
    "verify_github_projects",
]
