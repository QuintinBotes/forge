"""HARD-10 — the real OTLP export path (retires F38's in-memory-only asterisk).

Hermetic + network-free: installing providers constructs OTLP/HTTP exporters
that open no socket until they export, and these tests never emit a span/metric/
log, so every provider's queue is empty and ``shutdown`` flushes nothing. We
assert the wiring (real providers installed, idempotent, degrades cleanly),
not a live roundtrip — that is the networked gate (AC17).
"""

from __future__ import annotations

import pytest

from forge_obs.metrics import NoopMetrics, RecordingMetrics, reset_metrics
from forge_obs.otel_export import install_otel, otel_sdk_available, shutdown_otel
from forge_obs.settings import ObsSettings
from forge_obs.telemetry import setup_telemetry, shutdown_telemetry

ENDPOINT = "http://otel-collector.invalid:4318"


@pytest.fixture(autouse=True)
def _isolate():
    yield
    shutdown_telemetry()
    reset_metrics()


def test_sdk_is_available() -> None:
    # HARD-10 added opentelemetry-sdk + otlp-proto-http as first-class deps.
    assert otel_sdk_available() is True


def test_install_otel_returns_none_when_disabled() -> None:
    assert install_otel(ObsSettings(enabled=False, otlp_endpoint=ENDPOINT)) is None


def test_install_otel_returns_none_without_endpoint() -> None:
    assert install_otel(ObsSettings(enabled=True, otlp_endpoint=None)) is None


def test_install_otel_wires_all_three_providers() -> None:
    providers = install_otel(ObsSettings(enabled=True, otlp_endpoint=ENDPOINT))
    try:
        assert providers is not None
        assert providers.tracer_provider is not None
        assert providers.meter_provider is not None
        assert providers.logger_provider is not None
    finally:
        shutdown_otel(providers)


def test_setup_telemetry_with_endpoint_is_exporting() -> None:
    handle = setup_telemetry(
        "forge-api", ObsSettings(enabled=True, otlp_endpoint=ENDPOINT, version="9.9.9")
    )
    assert handle.enabled is True
    assert handle.exporting is True
    assert handle.otel is not None
    assert isinstance(handle.metrics, RecordingMetrics)


def test_setup_telemetry_enabled_without_endpoint_records_but_does_not_export() -> None:
    handle = setup_telemetry("forge-worker", ObsSettings(enabled=True, otlp_endpoint=None))
    assert handle.enabled is True
    assert handle.exporting is False  # recorder only; no OTLP push attempted
    assert isinstance(handle.metrics, RecordingMetrics)


def test_setup_telemetry_disabled_is_noop_and_not_exporting() -> None:
    handle = setup_telemetry("forge-api", ObsSettings(enabled=False, otlp_endpoint=ENDPOINT))
    assert handle.enabled is False
    assert handle.exporting is False
    assert isinstance(handle.metrics, NoopMetrics)


def test_setup_telemetry_idempotent_does_not_reinstall() -> None:
    cfg = ObsSettings(enabled=True, otlp_endpoint=ENDPOINT)
    first = setup_telemetry("forge-api", cfg)
    second = setup_telemetry("forge-api", cfg)
    assert second is first  # same handle -> no duplicated providers/exporters


def test_shutdown_clears_exporting_state() -> None:
    handle = setup_telemetry("forge-api", ObsSettings(enabled=True, otlp_endpoint=ENDPOINT))
    assert handle.exporting is True
    handle.shutdown()
    assert handle.exporting is False
