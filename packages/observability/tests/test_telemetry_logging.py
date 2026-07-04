"""setup_telemetry idempotence + degraded mode (AC1/AC18) and JSON logging (AC12)."""

from __future__ import annotations

import json
import logging

import pytest

from forge_obs.logging import JsonLogFormatter, bind_context, clear_context, configure_logging
from forge_obs.metrics import NoopMetrics, RecordingMetrics, get_metrics, reset_metrics
from forge_obs.settings import ObsSettings
from forge_obs.telemetry import setup_telemetry, shutdown_telemetry
from forge_obs.tracing import traced


@pytest.fixture(autouse=True)
def _isolate():
    yield
    shutdown_telemetry()
    reset_metrics()
    clear_context()


def test_setup_disabled_installs_noop_and_attempts_no_export() -> None:
    handle = setup_telemetry("forge-api", ObsSettings(enabled=False))
    assert isinstance(handle.metrics, NoopMetrics)
    assert isinstance(get_metrics(), NoopMetrics)
    assert handle.enabled is False


def test_setup_enabled_installs_recording_metrics() -> None:
    handle = setup_telemetry("forge-api", ObsSettings(enabled=True))
    assert isinstance(handle.metrics, RecordingMetrics)
    assert get_metrics() is handle.metrics
    assert handle.enabled is True


def test_setup_is_idempotent_same_settings_same_handle() -> None:
    cfg = ObsSettings(enabled=True)
    first = setup_telemetry("forge-api", cfg)
    second = setup_telemetry("forge-api", cfg)
    assert second is first  # no duplicated instruments (AC1)


def test_settings_from_env_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("OBS_ENABLED", "OTEL_EXPORTER_OTLP_ENDPOINT"):
        monkeypatch.delenv(var, raising=False)
    cfg = ObsSettings.from_env("forge-worker")
    assert cfg.enabled is False
    assert cfg.otlp_endpoint is None
    monkeypatch.setenv("OBS_ENABLED", "true")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")
    cfg2 = ObsSettings.from_env("forge-worker")
    assert cfg2.enabled is True and cfg2.otlp_endpoint == "http://otel-collector:4317"


def _fmt(record: logging.LogRecord) -> dict:
    return json.loads(JsonLogFormatter("forge-api").format(record))


def _record(msg: str, **extra) -> logging.LogRecord:
    record = logging.LogRecord("test.logger", logging.INFO, __file__, 1, msg, (), None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_log_shape_and_context() -> None:
    bind_context(workspace_id="00000000-0000-0000-0000-0000000000a1")
    payload = _fmt(_record("hello world"))
    assert payload["service"] == "forge-api"
    assert payload["level"] == "info"
    assert payload["msg"] == "hello world"
    assert payload["workspace_id"] == "00000000-0000-0000-0000-0000000000a1"


def test_log_inside_span_carries_trace_context() -> None:
    with traced("knowledge.search") as span:
        payload = _fmt(_record("searching"))
    assert payload["trace_id"] == span.trace_id
    assert payload["span_id"] == span.span_id


def test_log_secret_redaction_runs_last_nested() -> None:
    payload = _fmt(
        _record(
            "calling provider with API_KEY=sk-anthropic123456789012345",
            details={"headers": {"authorization": "Bearer abcdef1234567890XYZ"}},
        )
    )
    text = json.dumps(payload)
    assert "sk-anthropic123456789012345" not in text
    assert "abcdef1234567890XYZ" not in text
    assert "[REDACTED]" in text


def test_structural_trace_fields_survive_redaction(monkeypatch: pytest.MonkeyPatch) -> None:
    """A trace_id the entropy heuristic WOULD scrub still survives (HARD-10 AC17).

    trace_id/span_id are W3C correlation ids, not secrets; they must reach the
    sink verbatim so an operator can pivot Grafana panel -> Tempo trace -> Loki
    logs. User content in the same record is still redacted.
    """
    from forge_obs import logging as obs_logging
    from forge_obs.redaction import redact_text

    redactable = "5b9b4f4ffda7d63cce99145c08c243e0"
    assert redact_text(redactable) != redactable  # confirms the heuristic scrubs it bare
    monkeypatch.setattr(obs_logging, "current_trace_id", lambda: redactable)
    monkeypatch.setattr(obs_logging, "current_span_id", lambda: "b7ad6b7169203331")

    payload = _fmt(_record("calling provider with API_KEY=sk-anthropic123456789012345"))
    assert payload["trace_id"] == redactable  # correlation id survived redaction
    assert payload["span_id"] == "b7ad6b7169203331"
    # ...while the secret in the message is still gone.
    assert "sk-anthropic123456789012345" not in json.dumps(payload)
    assert "[REDACTED]" in json.dumps(payload)


def test_configure_logging_idempotent() -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    configure_logging(service_name="forge-api")
    configure_logging(service_name="forge-api")
    added = [h for h in root.handlers if h not in before]
    assert len(added) <= 1
    for handler in added:
        root.removeHandler(handler)
