"""Tests for OpenTelemetry span hooks (Task 1.14 — observability + audit).

Spec Tech Stack lists OpenTelemetry. The hooks degrade gracefully when the
``opentelemetry`` SDK is not installed (no-network sandbox): spans are still
recorded in-memory so traces and tests work, and attributes are redacted.
"""

from __future__ import annotations

import pytest

from forge_api.observability.otel import (
    OTEL_AVAILABLE,
    SpanRecorder,
    span,
    traced,
)
from forge_api.observability.redaction import REDACTED


def test_otel_available_is_a_bool() -> None:
    assert isinstance(OTEL_AVAILABLE, bool)


def test_span_records_name_and_duration() -> None:
    rec = SpanRecorder()
    with span("index_repo", recorder=rec):
        pass
    spans = rec.recorded_spans()
    assert len(spans) == 1
    assert spans[0].name == "index_repo"
    assert spans[0].duration_ms is not None
    assert spans[0].duration_ms >= 0


def test_nested_spans_capture_parent() -> None:
    rec = SpanRecorder()
    with span("outer", recorder=rec), span("inner", recorder=rec):
        pass
    by_name = {s.name: s for s in rec.recorded_spans()}
    assert by_name["inner"].parent == "outer"
    assert by_name["outer"].parent is None


def test_span_attributes_are_redacted() -> None:
    rec = SpanRecorder()
    with span("call", {"api_key": "sk-SECRET1234567890", "kind": "search"}, recorder=rec):
        pass
    attrs = rec.recorded_spans()[0].attributes
    assert attrs["api_key"] == REDACTED
    assert attrs["kind"] == "search"


def test_span_records_even_on_exception_and_marks_error() -> None:
    rec = SpanRecorder()
    with pytest.raises(ValueError), span("boom", recorder=rec):
        raise ValueError("kaboom")
    spans = rec.recorded_spans()
    assert len(spans) == 1
    assert spans[0].status == "error"


def test_traced_decorator_records_span_and_returns_value() -> None:
    rec = SpanRecorder()

    @traced("compute", recorder=rec)
    def compute(a: int, b: int) -> int:
        return a + b

    assert compute(2, 3) == 5
    spans = rec.recorded_spans()
    assert len(spans) == 1
    assert spans[0].name == "compute"


def test_recorder_clear_empties_spans() -> None:
    rec = SpanRecorder()
    with span("x", recorder=rec):
        pass
    rec.clear()
    assert rec.recorded_spans() == []
