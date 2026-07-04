"""Lightweight spans with W3C-shaped ids for log/trace correlation (F38).

Real OTLP span export is PARKED until the OpenTelemetry SDK dependency lands
(no third-party network in this build environment); the frozen surface here —
``traced()``, ``current_trace_id()``, ``current_span_id()`` — is what producers
and the logging pipeline consume, so swapping the backend for the OTel SDK
later changes no caller. Ids follow the W3C trace-context shape (32/16 hex) so
exported spans will correlate with these logs unchanged.

Span attributes are redacted before recording (secrets never reach a sink) and
may legitimately carry high-cardinality dimensions (task/run/workspace ids) —
that is exactly where the cardinality policy sends them (spec §4).
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from forge_obs.redaction import redact_value

__all__ = ["Span", "SpanStore", "current_span_id", "current_trace_id", "get_span_store", "traced"]


@dataclass
class Span:
    """One finished (or live) span record."""

    name: str
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    start_s: float = 0.0
    duration_s: float | None = None
    status: str = "ok"


class SpanStore:
    """In-memory sink for finished spans (tests + the run-trace debug surface)."""

    def __init__(self) -> None:
        self._spans: list[Span] = []

    def add(self, span: Span) -> None:
        self._spans.append(span)

    def spans(self) -> list[Span]:
        return list(self._spans)

    def clear(self) -> None:
        self._spans.clear()


_STORE = SpanStore()
_CURRENT: ContextVar[Span | None] = ContextVar("forge_obs_current_span", default=None)


def get_span_store() -> SpanStore:
    """The process-wide span sink."""
    return _STORE


def current_trace_id() -> str | None:
    span = _CURRENT.get()
    return span.trace_id if span else None


def current_span_id() -> str | None:
    span = _CURRENT.get()
    return span.span_id if span else None


@contextmanager
def traced(
    name: str,
    *,
    store: SpanStore | None = None,
    **attributes: Any,
) -> Iterator[Span]:
    """Open a span: child spans share the parent's ``trace_id``.

    Attributes are secret-redacted before they are recorded. The span is added
    to the store even when the wrapped block raises (status ``error``).
    """
    parent = _CURRENT.get()
    span = Span(
        name=name,
        trace_id=parent.trace_id if parent else secrets.token_hex(16),
        span_id=secrets.token_hex(8),
        parent_span_id=parent.span_id if parent else None,
        attributes=redact_value(dict(attributes)),
        start_s=time.perf_counter(),
    )
    token = _CURRENT.set(span)
    try:
        yield span
    except BaseException:
        span.status = "error"
        raise
    finally:
        span.duration_s = time.perf_counter() - span.start_s
        _CURRENT.reset(token)
        (store or _STORE).add(span)
