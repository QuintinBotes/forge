"""Grafana alerting adapter (F17)."""

from __future__ import annotations

from forge_contracts.enums import IncidentSeverity
from forge_contracts.incident import AlertProvider, IncidentAlert
from forge_integrations.alerts.base import BaseAlertAdapter, parse_json_body

__all__ = ["GrafanaAlertAdapter"]

_SEVERITY = {
    "critical": IncidentSeverity.CRITICAL,
    "high": IncidentSeverity.HIGH,
    "error": IncidentSeverity.HIGH,
    "warning": IncidentSeverity.MEDIUM,
    "medium": IncidentSeverity.MEDIUM,
    "info": IncidentSeverity.LOW,
    "low": IncidentSeverity.LOW,
}


class GrafanaAlertAdapter(BaseAlertAdapter):
    """Normalizes a Grafana unified-alerting webhook into an :class:`IncidentAlert`."""

    provider = AlertProvider.GRAFANA
    signature_header = "X-Grafana-Signature"

    def normalize(self, *, body: bytes, headers: dict[str, str]) -> IncidentAlert:
        payload = parse_json_body(body)
        alerts = payload.get("alerts") or []
        first = alerts[0] if alerts else {}
        labels = {**(payload.get("commonLabels") or {}), **(first.get("labels") or {})}
        annotations = {
            **(payload.get("commonAnnotations") or {}),
            **(first.get("annotations") or {}),
        }
        fingerprint = str(first.get("fingerprint") or "")
        dedup_key = str(
            fingerprint or payload.get("groupKey") or labels.get("alertname") or "grafana"
        )
        # Grafana carries no native severity; read a `severity` label, else map
        # the alert state (firing -> high).
        severity_label = labels.get("severity")
        if severity_label:
            severity = self._map_severity(severity_label, _SEVERITY)
        else:
            state = (payload.get("status") or first.get("status") or "").lower()
            severity = IncidentSeverity.HIGH if state == "firing" else IncidentSeverity.LOW
        return IncidentAlert(
            provider=self.provider,
            external_id=fingerprint or None,
            delivery_id=self._delivery_id(headers, "X-Grafana-Delivery-Id", "X-Request-Id"),
            dedup_key=dedup_key,
            title=str(
                labels.get("alertname")
                or annotations.get("summary")
                or payload.get("title")
                or "Grafana alert"
            ),
            severity=severity,
            service=labels.get("service") or labels.get("job"),
            description=str(annotations.get("description") or "") or None,
        )
