"""OpenTelemetry span hooks (Task 1.14 — observability + audit).

Spec Tech Stack lists OpenTelemetry for tracing. To keep Phase-1 hermetic and
runnable in a no-network sandbox, these hooks degrade gracefully:

- If the ``opentelemetry`` SDK is installed, spans are also emitted to the real
  tracer.
- Always, spans are recorded in an in-memory :class:`SpanRecorder` (so the trace
  viewer and tests work without a collector).
- Span attributes are redacted before recording, per the secret-redaction rule.

Spans nest via a context variable so parent/child relationships are captured.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from forge_api.observability.redaction import redact_mapping

try:  # pragma: no cover - exercised only when the SDK is installed
    from opentelemetry import trace as _otel_trace

    OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - default in the sandbox
    _otel_trace = None
    OTEL_AVAILABLE = False

F = TypeVar("F", bound=Callable[..., Any])


class SpanRecord(BaseModel):
    """An in-memory record of a single span."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    parent: str | None = None
    start_ms: float
    end_ms: float | None = None
    duration_ms: float | None = None
    status: str = "ok"


class SpanRecorder:
    """Collects :class:`SpanRecord`s in memory for inspection and the trace UI."""

    def __init__(self) -> None:
        self._spans: list[SpanRecord] = []

    def add(self, record: SpanRecord) -> None:
        self._spans.append(record)

    def recorded_spans(self) -> list[SpanRecord]:
        return list(self._spans)

    def clear(self) -> None:
        self._spans.clear()


#: Process-wide default recorder; callers may pass their own for isolation.
_DEFAULT_RECORDER = SpanRecorder()

#: Stack of active span names for parent/child linkage.
_SPAN_STACK: ContextVar[tuple[str, ...]] = ContextVar("forge_span_stack", default=())


def get_span_recorder() -> SpanRecorder:
    """Return the process-wide default span recorder."""
    return _DEFAULT_RECORDER


def get_tracer() -> Any | None:
    """Return the real OpenTelemetry tracer when available, else ``None``."""
    if OTEL_AVAILABLE:
        return _otel_trace.get_tracer("forge")
    return None


def _coerce_attr(value: Any) -> Any:
    """OTel attributes must be primitive; stringify anything else."""
    if isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


@contextmanager
def span(
    name: str,
    attributes: dict[str, Any] | None = None,
    *,
    recorder: SpanRecorder | None = None,
    redact: bool = True,
) -> Iterator[SpanRecord]:
    """Context manager recording a (possibly nested) span.

    Yields the live :class:`SpanRecord`; its duration and status are finalised on
    exit. The record is added to ``recorder`` even when the wrapped block raises.
    """
    sink = recorder or _DEFAULT_RECORDER
    attrs = attributes or {}
    safe_attrs = redact_mapping(attrs) if redact else dict(attrs)

    stack = _SPAN_STACK.get()
    parent = stack[-1] if stack else None
    token = _SPAN_STACK.set((*stack, name))

    record = SpanRecord(
        name=name,
        attributes=safe_attrs,
        parent=parent,
        start_ms=time.perf_counter() * 1000.0,
    )

    otel_cm = None
    otel_span = None
    tracer = get_tracer()
    if tracer is not None:  # pragma: no cover - only with SDK installed
        otel_cm = tracer.start_as_current_span(name)
        otel_span = otel_cm.__enter__()
        for key, value in safe_attrs.items():
            otel_span.set_attribute(key, _coerce_attr(value))

    try:
        yield record
    except BaseException as exc:
        record.status = "error"
        if otel_span is not None:  # pragma: no cover - only with SDK installed
            otel_span.record_exception(exc)
        raise
    finally:
        record.end_ms = time.perf_counter() * 1000.0
        record.duration_ms = record.end_ms - record.start_ms
        sink.add(record)
        _SPAN_STACK.reset(token)
        if otel_cm is not None:  # pragma: no cover - only with SDK installed
            otel_cm.__exit__(None, None, None)


def traced(name: str | None = None, *, recorder: SpanRecorder | None = None) -> Callable[[F], F]:
    """Decorator wrapping a callable in a :func:`span`."""

    def decorator(fn: F) -> F:
        span_name = name or fn.__qualname__

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with span(span_name, recorder=recorder):
                return fn(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator
