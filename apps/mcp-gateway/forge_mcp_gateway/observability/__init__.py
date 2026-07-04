"""MCP-gateway observability wiring (HARD-10 §3.3).

One telemetry init per gateway process (real OTLP export when enabled, a no-op
otherwise) that accepts inbound W3C trace context so an ``api``-started trace
continues through a gateway tool call, and the per-call metric emission
(``forge_mcp_calls_total`` + ``forge_mcp_call_latency_seconds``).
"""

from __future__ import annotations

from forge_mcp_gateway.observability.init import (
    extract_trace_context,
    record_tool_call,
    setup_gateway_telemetry,
)

__all__ = ["extract_trace_context", "record_tool_call", "setup_gateway_telemetry"]
