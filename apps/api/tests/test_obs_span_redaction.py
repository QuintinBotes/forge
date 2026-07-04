"""Structural trace-redaction tests (HARD-13 AC12).

An OTel ``SpanProcessor`` scrubs span attributes before export, so a secret set as
a span attribute anywhere never reaches a collector.
"""

from __future__ import annotations

import pytest

pytest.importorskip("opentelemetry.sdk.trace")

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from forge_api.observability.otel import RedactingSpanProcessor


def test_span_processor_redacts_secret_attribute_before_export() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(RedactingSpanProcessor(exporter))
    tracer = provider.get_tracer("hard13.test")

    with tracer.start_as_current_span("resolve-secret") as span:
        span.set_attribute("authorization", "Bearer sk-deadbeefdeadbeef00")
        span.set_attribute("http.method", "GET")

    provider.force_flush()
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    # Non-secret attributes are preserved untouched.
    assert attrs["http.method"] == "GET"
    # The secret-named attribute is redacted, and the token never appears.
    assert attrs["authorization"] == "[REDACTED]"
    assert "sk-deadbeefdeadbeef00" not in str(attrs)


def test_span_processor_redacts_secret_shaped_value() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(RedactingSpanProcessor(exporter))
    tracer = provider.get_tracer("hard13.test")

    with tracer.start_as_current_span("call") as span:
        span.set_attribute("note", "using token ghp_abcdefghijklmnopqrstuvwxyz0123")

    provider.force_flush()
    span = exporter.get_finished_spans()[0]
    note = dict(span.attributes or {})["note"]
    assert "[REDACTED]" in note
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123" not in note
