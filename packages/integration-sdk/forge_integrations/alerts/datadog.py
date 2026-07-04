"""Datadog alert adapter (F17)."""

from __future__ import annotations

from forge_contracts.enums import IncidentSeverity
from forge_contracts.incident import AlertProvider, IncidentAlert
from forge_integrations.alerts.base import BaseAlertAdapter, parse_json_body

__all__ = ["DatadogAlertAdapter"]

_SEVERITY = {
    "p1": IncidentSeverity.CRITICAL,
    "p2": IncidentSeverity.HIGH,
    "p3": IncidentSeverity.MEDIUM,
    "p4": IncidentSeverity.LOW,
    "p5": IncidentSeverity.LOW,
    "critical": IncidentSeverity.CRITICAL,
    "error": IncidentSeverity.HIGH,
    "warning": IncidentSeverity.MEDIUM,
    "info": IncidentSeverity.LOW,
    "success": IncidentSeverity.LOW,
}


class DatadogAlertAdapter(BaseAlertAdapter):
    """Normalizes a Datadog monitor webhook into an :class:`IncidentAlert`."""

    provider = AlertProvider.DATADOG
    signature_header = "X-Datadog-Signature"

    def normalize(self, *, body: bytes, headers: dict[str, str]) -> IncidentAlert:
        payload = parse_json_body(body)
        alert_id = str(payload.get("id") or payload.get("alert_id") or "")
        dedup_key = str(
            payload.get("aggreg_key")
            or payload.get("alert_id")
            or alert_id
            or payload.get("title")
            or "datadog"
        )
        severity = self._map_severity(
            payload.get("priority") or payload.get("alert_type"), _SEVERITY
        )
        return IncidentAlert(
            provider=self.provider,
            external_id=alert_id or None,
            delivery_id=self._delivery_id(headers, "X-Datadog-Delivery-Id", "X-Request-Id"),
            dedup_key=dedup_key,
            title=str(payload.get("title") or payload.get("event_title") or "Datadog alert"),
            severity=severity,
            service=payload.get("scope") or payload.get("host"),
            description=str(payload.get("body") or payload.get("text") or "") or None,
        )
