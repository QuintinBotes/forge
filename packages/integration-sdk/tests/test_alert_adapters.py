"""Alert adapter verify/normalize tests over recorded fixtures (F17, AC4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from forge_contracts.enums import IncidentSeverity
from forge_contracts.incident import AlertProvider
from forge_integrations.alerts import (
    DatadogAlertAdapter,
    GrafanaAlertAdapter,
    PagerDutyAlertAdapter,
    SentryAlertAdapter,
    get_alert_adapter,
)

FIXTURES = Path(__file__).parent / "fixtures" / "alerts"

CASES = [
    (PagerDutyAlertAdapter(), "pagerduty.json", IncidentSeverity.CRITICAL, "checkout-api:5xx"),
    (DatadogAlertAdapter(), "datadog.json", IncidentSeverity.CRITICAL, "checkout-api:latency"),
    (SentryAlertAdapter(), "sentry.json", IncidentSeverity.HIGH, "ISSUE-42"),
    (GrafanaAlertAdapter(), "grafana.json", IncidentSeverity.CRITICAL, "abc123fingerprint"),
]


@pytest.mark.parametrize(("adapter", "fixture", "severity", "dedup"), CASES)
def test_normalize_from_fixture(adapter, fixture: str, severity, dedup: str) -> None:
    body = (FIXTURES / fixture).read_bytes()
    alert = adapter.normalize(body=body, headers={})
    assert alert.provider is adapter.provider
    assert alert.severity is severity
    assert alert.dedup_key == dedup
    assert alert.title


@pytest.mark.parametrize(("adapter", "fixture", "severity", "dedup"), CASES)
def test_verify_good_bad_and_missing_signature(adapter, fixture: str, severity, dedup: str) -> None:
    body = (FIXTURES / fixture).read_bytes()
    secret = "shh-secret"
    good = adapter.sign(secret, body)
    header_name = adapter.signature_header
    # Good signature.
    assert adapter.verify(secret=secret, body=body, headers={header_name: good}) is True
    # Bad signature.
    assert (
        adapter.verify(
            secret=secret, body=body, headers={header_name: adapter.signature_prefix + "deadbeef"}
        )
        is False
    )
    # Missing signature.
    assert adapter.verify(secret=secret, body=body, headers={}) is False
    # Empty secret -> fail closed.
    assert adapter.verify(secret="", body=body, headers={header_name: good}) is False
    # Tampered body invalidates an otherwise-good signature.
    assert adapter.verify(secret=secret, body=body + b" ", headers={header_name: good}) is False


def test_registry_lookup() -> None:
    assert isinstance(get_alert_adapter(AlertProvider.SENTRY), SentryAlertAdapter)
    assert isinstance(get_alert_adapter("datadog"), DatadogAlertAdapter)
    with pytest.raises(KeyError):
        get_alert_adapter(AlertProvider.MANUAL)
