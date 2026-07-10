"""Generic/BYO-board webhook verification + parsing, driven by ``GenericWebhookConfig``.

Supports the three signature schemes seen across F18/F40's native adapters —
a constant-time shared-secret header (Jira/monday.com/GitLab), an HMAC-SHA256
hex digest (Asana/ClickUp/GitHub), and an HMAC-SHA1 base64 digest
(Trello) — plus ``none`` for boards with no verifiable webhook at all
(the delivery is still accepted as an unauthenticated hint; state is always
re-fetched, never trusted, matching the Protocol's documented "hint only"
contract).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import UTC, datetime
from typing import Any

from forge_contracts.pm import (
    GenericWebhookConfig,
    GenericWebhookSignatureAlgo,
    PMProvider,
    WebhookEvent,
)
from forge_integrations.pm.generic.paths import get_path


def verify_generic(
    config: GenericWebhookConfig, secret: str, body: bytes, headers: dict[str, str]
) -> bool:
    if config.signature_algo is GenericWebhookSignatureAlgo.none:
        return True
    if not config.signature_header:
        return False
    lowered = {k.lower(): v for k, v in headers.items()}
    provided = lowered.get(config.signature_header.lower())
    if not secret or not provided:
        return False
    if config.signature_algo is GenericWebhookSignatureAlgo.shared_secret_header:
        return hmac.compare_digest(secret, provided)
    if config.signature_algo is GenericWebhookSignatureAlgo.hmac_sha256_hex:
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, provided)
    if config.signature_algo is GenericWebhookSignatureAlgo.hmac_sha1_base64:
        digest = hmac.new(secret.encode(), body, hashlib.sha1).digest()
        expected = base64.b64encode(digest).decode()
        return hmac.compare_digest(expected, provided)
    return False  # pragma: no cover - exhaustive over the StrEnum


def synthesize_delivery_id(body: bytes, *, received_at: datetime | None = None) -> str:
    now = received_at or datetime.now(UTC)
    minute = now.strftime("%Y%m%d%H%M")
    return f"{hashlib.sha256(body).hexdigest()}:{minute}"


def parse_generic(
    config: GenericWebhookConfig, body: dict[str, Any], *, delivery_id: str, signature_valid: bool
) -> WebhookEvent:
    raw_event = get_path(body, config.event_type_path)
    event_type = config.event_type_map.get(str(raw_event), config.default_event_type)
    external_id = get_path(body, config.external_id_path)
    return WebhookEvent(
        provider=PMProvider.generic,
        delivery_id=delivery_id,
        event_type=event_type,
        external_id=str(external_id) if external_id is not None else None,
        external_key=str(external_id) if external_id is not None else None,
        signature_valid=signature_valid,
        received_at=datetime.now(UTC),
        payload={"raw_event": raw_event},
    )


__all__ = ["parse_generic", "synthesize_delivery_id", "verify_generic"]
