"""Alert normalization + dedup-key derivation (F17).

Provider-specific webhook parsing lives in the integration-sdk alert adapters;
this module owns the provider-agnostic normalization the incident service relies
on: deriving a stable dedup key and ensuring a severity.
"""

from __future__ import annotations

import hashlib

from forge_contracts.enums import IncidentSeverity
from forge_contracts.incident import IncidentAlert

__all__ = ["AlertNormalizer", "derive_dedup_key"]


def derive_dedup_key(alert: IncidentAlert) -> str:
    """Return a stable dedup key for ``alert``.

    Uses the alert's explicit ``dedup_key`` when present; otherwise derives one
    deterministically from ``(provider, service, external_id|title)`` so repeat
    deliveries of the same condition collapse onto one open incident.
    """
    if alert.dedup_key:
        return alert.dedup_key
    parts = [alert.provider.value, alert.service or "", alert.external_id or alert.title]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]
    return f"{alert.provider.value}:{digest}"


class AlertNormalizer:
    """Normalizes a raw :class:`IncidentAlert` for the incident service."""

    def normalize(self, alert: IncidentAlert) -> IncidentAlert:
        """Return a copy of ``alert`` with a guaranteed dedup key + severity."""
        severity = alert.severity or IncidentSeverity.MEDIUM
        return alert.model_copy(
            update={"dedup_key": derive_dedup_key(alert), "severity": severity}
        )
