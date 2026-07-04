"""HARD-10 — gateway telemetry init, inbound trace context, MCP call metrics."""

from __future__ import annotations

from forge_mcp_gateway.observability import (
    extract_trace_context,
    record_tool_call,
    setup_gateway_telemetry,
)
from forge_obs.metrics import RecordingMetrics, reset_metrics, set_metrics
from forge_obs.settings import ObsSettings
from forge_obs.telemetry import shutdown_telemetry


def teardown_function() -> None:
    shutdown_telemetry()
    reset_metrics()


def test_setup_gateway_telemetry_names_service() -> None:
    handle = setup_gateway_telemetry(ObsSettings(enabled=False))
    assert handle.service_name == "forge-mcp-gateway"


def test_extract_trace_context_picks_w3c_headers_case_insensitive() -> None:
    headers = {
        "TraceParent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
        "TraceState": "forge=1",
        "x-other": "ignored",
    }
    ctx = extract_trace_context(headers)
    assert ctx == {
        "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
        "tracestate": "forge=1",
    }


def test_extract_trace_context_empty_when_absent() -> None:
    assert extract_trace_context({"content-type": "application/json"}) == {}


def test_record_tool_call_increments_facade() -> None:
    metrics = RecordingMetrics(service="forge-mcp-gateway")
    set_metrics(metrics)
    record_tool_call(connection="github", status="ok", latency_seconds=0.25)
    assert metrics.counter_value("forge_mcp_calls_total", connection="github", status="ok") == 1
    assert metrics.histogram_values("forge_mcp_call_latency_seconds", connection="github") == [0.25]


def test_record_tool_call_never_raises_on_bad_facade() -> None:
    class _Boom:
        def record_mcp_call(self, **_kwargs):
            raise RuntimeError("boom")

    set_metrics(_Boom())
    # Guarded: a facade failure must not propagate out of a tool call.
    record_tool_call(connection="github", status="error", latency_seconds=0.1)
