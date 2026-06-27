"""Shared base for provider alert adapters (F17).

Each adapter maps a provider webhook to a normalized
:class:`forge_contracts.incident.IncidentAlert` and verifies the request
signature over the *exact raw bytes* (no body buffering/rewrite). Adapters are
pure (no network) and recorded-fixture tested.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from forge_contracts.enums import IncidentSeverity
from forge_contracts.incident import AlertProvider, IncidentAlert

__all__ = ["BaseAlertAdapter", "lower_headers", "parse_json_body"]


def lower_headers(headers: dict[str, str]) -> dict[str, str]:
    """Return a case-insensitive (lower-keyed) copy of ``headers``."""
    return {str(k).lower(): v for k, v in (headers or {}).items()}


def parse_json_body(body: bytes) -> dict[str, Any]:
    """Strictly parse a JSON object webhook body (raises on malformed input)."""
    data = json.loads(body.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("alert webhook body must be a JSON object")
    return data


class BaseAlertAdapter:
    """Base adapter: HMAC-SHA256 signature verification + helpers.

    Subclasses set ``provider`` / ``signature_header`` / ``signature_prefix`` and
    implement :meth:`normalize`.
    """

    provider: AlertProvider
    signature_header: str
    signature_prefix: str = ""

    def sign(self, secret: str, body: bytes) -> str:
        """Compute the expected signature header value for ``body``."""
        digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        return f"{self.signature_prefix}{digest}"

    def verify(self, *, secret: str, body: bytes, headers: dict[str, str]) -> bool:
        """Constant-time verify the provider signature over the raw body."""
        if not secret:
            return False
        provided = lower_headers(headers).get(self.signature_header.lower())
        if not provided:
            return False
        expected = self.sign(secret, body)
        # Some providers send a comma-separated list of accepted signatures.
        for candidate in provided.split(","):
            if hmac.compare_digest(expected, candidate.strip()):
                return True
        return False

    def normalize(self, *, body: bytes, headers: dict[str, str]) -> IncidentAlert:
        raise NotImplementedError

    # -- helpers ---------------------------------------------------------- #

    @staticmethod
    def _delivery_id(headers: dict[str, str], *names: str) -> str | None:
        low = lower_headers(headers)
        for name in names:
            value = low.get(name.lower())
            if value:
                return value
        return None

    @staticmethod
    def _map_severity(value: str | None, mapping: dict[str, IncidentSeverity]) -> IncidentSeverity:
        return mapping.get((value or "").strip().lower(), IncidentSeverity.MEDIUM)
