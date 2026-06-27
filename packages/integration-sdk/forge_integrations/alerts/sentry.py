"""Sentry alert adapter (F17)."""

from __future__ import annotations

from forge_contracts.enums import IncidentSeverity
from forge_contracts.incident import AlertProvider, IncidentAlert
from forge_integrations.alerts.base import BaseAlertAdapter, parse_json_body

__all__ = ["SentryAlertAdapter"]

_SEVERITY = {
    "fatal": IncidentSeverity.CRITICAL,
    "error": IncidentSeverity.HIGH,
    "warning": IncidentSeverity.MEDIUM,
    "info": IncidentSeverity.LOW,
    "debug": IncidentSeverity.LOW,
}


class SentryAlertAdapter(BaseAlertAdapter):
    """Normalizes a Sentry issue-alert webhook into an :class:`IncidentAlert`."""

    provider = AlertProvider.SENTRY
    signature_header = "Sentry-Hook-Signature"

    def normalize(self, *, body: bytes, headers: dict[str, str]) -> IncidentAlert:
        payload = parse_json_body(body)
        data = payload.get("data") or {}
        event = data.get("event") or data.get("issue") or {}
        external_id = str(event.get("id") or event.get("issue_id") or "")
        dedup_key = str(
            event.get("issue_id")
            or event.get("culprit")
            or external_id
            or event.get("title")
            or "sentry"
        )
        severity = self._map_severity(event.get("level"), _SEVERITY)
        project = event.get("project") or payload.get("project")
        return IncidentAlert(
            provider=self.provider,
            external_id=external_id or None,
            delivery_id=self._delivery_id(headers, "Sentry-Hook-Resource-Id", "Request-Id"),
            dedup_key=dedup_key,
            title=str(event.get("title") or event.get("message") or "Sentry issue"),
            severity=severity,
            service=str(project) if project else None,
            description=str(event.get("culprit") or "") or None,
        )
