"""Gateway telemetry init, inbound trace context, and MCP call metrics (HARD-10).

``setup_gateway_telemetry`` installs the env-driven telemetry providers (real
OTLP export + W3C propagation when enabled, no-op otherwise).

``extract_trace_context`` reads the inbound ``traceparent``/``tracestate``
headers (best-effort — a real span continuation happens through the OTel
FastAPI instrumentor when installed) so an ``api``-initiated trace stitches
through a gateway tool call into one end-to-end trace.

``record_tool_call`` is the single MCP metric emission point: it stamps
``forge_mcp_calls_total{connection,status}`` + ``forge_mcp_call_latency_seconds``
through the bounded-cardinality facade. The ``connection`` label is the
connection *name* (guarded), never a payload; emission never raises.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping

from forge_obs.metrics import get_metrics
from forge_obs.settings import ObsSettings
from forge_obs.telemetry import Telemetry, setup_telemetry

__all__ = ["extract_trace_context", "record_tool_call", "setup_gateway_telemetry"]

_SERVICE = "forge-mcp-gateway"


def setup_gateway_telemetry(settings: ObsSettings | None = None) -> Telemetry:
    """Install gateway telemetry (idempotent; real export when enabled)."""
    return setup_telemetry(_SERVICE, settings)


def extract_trace_context(headers: Mapping[str, str]) -> dict[str, str]:
    """Return the inbound W3C trace-context headers present on the request."""
    lowered = {k.lower(): v for k, v in headers.items()}
    return {key: lowered[key] for key in ("traceparent", "tracestate") if key in lowered}


def record_tool_call(*, connection: str, status: str, latency_seconds: float) -> None:
    """Emit the MCP call metric through the facade (guarded; never raises)."""
    # Metric emission must never break a tool call (spec §8 guarded emission).
    with contextlib.suppress(Exception):
        get_metrics().record_mcp_call(
            connection=connection, status=status, latency_seconds=latency_seconds
        )
