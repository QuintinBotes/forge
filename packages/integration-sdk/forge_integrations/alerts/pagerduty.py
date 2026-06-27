"""PagerDuty alert adapter (F17)."""

from __future__ import annotations

from forge_contracts.enums import IncidentSeverity
from forge_contracts.incident import AlertProvider, IncidentAlert
from forge_integrations.alerts.base import BaseAlertAdapter, parse_json_body

__all__ = ["PagerDutyAlertAdapter"]

_SEVERITY = {
    "critical": IncidentSeverity.CRITICAL,
    "error": IncidentSeverity.HIGH,
    "high": IncidentSeverity.HIGH,
    "warning": IncidentSeverity.MEDIUM,
    "info": IncidentSeverity.LOW,
    "low": IncidentSeverity.LOW,
}


class PagerDutyAlertAdapter(BaseAlertAdapter):
    """Normalizes a PagerDuty v3 webhook event into an :class:`IncidentAlert`."""

    provider = AlertProvider.PAGERDUTY
    signature_header = "X-PagerDuty-Signature"
    signature_prefix = "v1="

    def normalize(self, *, body: bytes, headers: dict[str, str]) -> IncidentAlert:
        payload = parse_json_body(body)
        event = payload.get("event") or payload
        data = event.get("data") or {}
        external_id = str(data.get("id") or event.get("id") or "")
        dedup_key = str(
            data.get("dedup_key")
            or data.get("incident_key")
            or external_id
            or data.get("title")
            or "pagerduty"
        )
        severity = self._map_severity(
            data.get("severity") or data.get("priority", {}).get("summary")
            if isinstance(data.get("priority"), dict)
            else data.get("severity"),
            _SEVERITY,
        )
        service = None
        svc = data.get("service")
        if isinstance(svc, dict):
            service = svc.get("summary") or svc.get("name")
        return IncidentAlert(
            provider=self.provider,
            external_id=external_id or None,
            delivery_id=self._delivery_id(headers, "X-Webhook-Id", "X-PagerDuty-Delivery-Id"),
            dedup_key=dedup_key,
            title=str(data.get("title") or event.get("event_type") or "PagerDuty alert"),
            severity=severity,
            service=service,
            description=str(data.get("description") or "") or None,
        )
