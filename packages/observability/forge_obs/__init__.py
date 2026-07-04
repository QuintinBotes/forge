"""forge_obs — observability & cost metrics SDK (slice F38).

One shared telemetry init (:func:`setup_telemetry`), the typed Key-Metric
facade (:class:`ForgeMetrics` via :func:`get_metrics`), structured redacted
logging, lightweight trace correlation, and the single cost-emission path
(:class:`~forge_obs.cost.UsageMeter` over the durable cost ledger).
"""

from __future__ import annotations

from forge_obs.logging import bind_context, clear_context, configure_logging, get_logger
from forge_obs.metrics import (
    FORBIDDEN_LABELS,
    INSTRUMENT_CATALOG,
    ForgeMetrics,
    NoopMetrics,
    RecordingMetrics,
    get_metrics,
    render_prometheus,
    reset_metrics,
    set_metrics,
)
from forge_obs.otel_export import otel_sdk_available
from forge_obs.redaction import REDACTED, get_redactor, redact_text, redact_value
from forge_obs.settings import ObsSettings
from forge_obs.telemetry import Telemetry, setup_telemetry, shutdown_telemetry
from forge_obs.tracing import current_span_id, current_trace_id, get_span_store, traced

__all__ = [
    "FORBIDDEN_LABELS",
    "INSTRUMENT_CATALOG",
    "REDACTED",
    "ForgeMetrics",
    "NoopMetrics",
    "ObsSettings",
    "RecordingMetrics",
    "Telemetry",
    "bind_context",
    "clear_context",
    "configure_logging",
    "current_span_id",
    "current_trace_id",
    "get_logger",
    "get_metrics",
    "get_redactor",
    "get_span_store",
    "otel_sdk_available",
    "redact_text",
    "redact_value",
    "render_prometheus",
    "reset_metrics",
    "set_metrics",
    "setup_telemetry",
    "shutdown_telemetry",
    "traced",
]
