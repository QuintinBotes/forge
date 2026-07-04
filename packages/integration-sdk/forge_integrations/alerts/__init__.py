"""Provider alert adapters (F17).

Maps PagerDuty / Datadog / Sentry / Grafana webhooks to the normalized
:class:`forge_contracts.incident.IncidentAlert`. Adapters are pure (no network)
and signature-verify over the exact raw request bytes.
"""

from __future__ import annotations

from forge_contracts.incident import AlertProvider
from forge_integrations.alerts.base import BaseAlertAdapter
from forge_integrations.alerts.datadog import DatadogAlertAdapter
from forge_integrations.alerts.grafana import GrafanaAlertAdapter
from forge_integrations.alerts.pagerduty import PagerDutyAlertAdapter
from forge_integrations.alerts.sentry import SentryAlertAdapter

__all__ = [
    "ALERT_ADAPTERS",
    "BaseAlertAdapter",
    "DatadogAlertAdapter",
    "GrafanaAlertAdapter",
    "PagerDutyAlertAdapter",
    "SentryAlertAdapter",
    "get_alert_adapter",
]

#: Registry of the shipped provider adapters, keyed by provider.
ALERT_ADAPTERS: dict[AlertProvider, BaseAlertAdapter] = {
    AlertProvider.PAGERDUTY: PagerDutyAlertAdapter(),
    AlertProvider.DATADOG: DatadogAlertAdapter(),
    AlertProvider.SENTRY: SentryAlertAdapter(),
    AlertProvider.GRAFANA: GrafanaAlertAdapter(),
}


def get_alert_adapter(provider: AlertProvider | str) -> BaseAlertAdapter:
    """Return the adapter for ``provider``; raise ``KeyError`` when unsupported."""
    key = AlertProvider(provider) if not isinstance(provider, AlertProvider) else provider
    return ALERT_ADAPTERS[key]
