"""Observability + audit subsystem for the Forge API (plan Task 1.14).

Public surface:
- Immutable, queryable, hash-chained audit log (:mod:`.audit`).
- Step-level run-trace assembler for the trace viewer (:mod:`.trace`).
- OpenTelemetry span hooks with in-memory fallback (:mod:`.otel`).
- Secret redaction primitive shared by all of the above (:mod:`.redaction`).
- :class:`ObservabilityService` that the ``/observability/*`` routes depend on.
"""

from __future__ import annotations

from forge_api.observability.audit import (
    GENESIS_HASH,
    AuditCategory,
    AuditEntry,
    AuditLog,
    AuditStore,
    InMemoryAuditStore,
    MCPAuditSink,
    compute_payload_hash,
    verify_chain,
)
from forge_api.observability.otel import (
    OTEL_AVAILABLE,
    SpanRecord,
    SpanRecorder,
    get_span_recorder,
    get_tracer,
    span,
    traced,
)
from forge_api.observability.redaction import (
    REDACTED,
    redact_mapping,
    redact_text,
    redact_value,
)
from forge_api.observability.service import (
    ObservabilityService,
    RunNotFoundError,
    get_observability_service,
)
from forge_api.observability.trace import RunTrace, RunTraceAssembler

__all__ = [
    "GENESIS_HASH",
    "OTEL_AVAILABLE",
    "REDACTED",
    "AuditCategory",
    "AuditEntry",
    "AuditLog",
    "AuditStore",
    "InMemoryAuditStore",
    "MCPAuditSink",
    "ObservabilityService",
    "RunNotFoundError",
    "RunTrace",
    "RunTraceAssembler",
    "SpanRecord",
    "SpanRecorder",
    "compute_payload_hash",
    "get_observability_service",
    "get_span_recorder",
    "get_tracer",
    "redact_mapping",
    "redact_text",
    "redact_value",
    "span",
    "traced",
    "verify_chain",
]
