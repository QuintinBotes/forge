"""Real OpenTelemetry export pipeline (HARD-10 — retires F38's PARKED asterisk).

F38 shipped the telemetry *seam* but degraded to an in-memory recorder because
the OpenTelemetry SDK could not be added offline. HARD-10 makes the export real:
:func:`install_otel` stands up genuine ``TracerProvider`` / ``MeterProvider`` /
``LoggerProvider`` instances with OTLP/HTTP exporters, a parent-based ratio
sampler, a uniform resource identity, W3C trace-context propagation, and
best-effort auto-instrumentation (FastAPI / SQLAlchemy / httpx / Celery / Redis /
requests — each optional, wired only if its instrumentor is importable).

Everything is import-guarded: if the SDK is absent the module reports itself
unavailable and the caller (:func:`~forge_obs.telemetry.setup_telemetry`) falls
back to the no-op/in-memory path, so the hermetic suite stays network-free. The
OTLP/HTTP exporters open no socket at construction — only on export — so merely
installing providers is itself network-free; a live roundtrip is the networked
gate (AC17).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from forge_obs.settings import ObsSettings

__all__ = ["OtelProviders", "install_otel", "otel_sdk_available", "shutdown_otel"]

_log = logging.getLogger(__name__)

try:  # pragma: no cover - trivial import guard
    from opentelemetry import trace as _trace
    from opentelemetry._logs import set_logger_provider as _set_logger_provider
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.propagate import set_global_textmap
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    _OTEL_SDK = True
except Exception:  # pragma: no cover - SDK genuinely absent
    _OTEL_SDK = False


def otel_sdk_available() -> bool:
    """True when the OpenTelemetry SDK + OTLP/HTTP exporters are importable."""
    return _OTEL_SDK


@dataclass
class OtelProviders:
    """Handles to the installed SDK providers so they can be flushed + closed."""

    tracer_provider: Any = None
    meter_provider: Any = None
    logger_provider: Any = None
    log_handler: Any = None
    instrumented: list[str] = field(default_factory=list)


def _signal_url(base: str, signal: str) -> str:
    return f"{base.rstrip('/')}/v1/{signal}"


def _instrument_frameworks() -> list[str]:
    """Wire every OTel auto-instrumentor that happens to be installed (optional)."""
    done: list[str] = []
    # (module path, class name) — each is best-effort; a missing package is fine.
    candidates = (
        ("opentelemetry.instrumentation.fastapi", "FastAPIInstrumentor"),
        ("opentelemetry.instrumentation.sqlalchemy", "SQLAlchemyInstrumentor"),
        ("opentelemetry.instrumentation.httpx", "HTTPXClientInstrumentor"),
        ("opentelemetry.instrumentation.celery", "CeleryInstrumentor"),
        ("opentelemetry.instrumentation.redis", "RedisInstrumentor"),
        ("opentelemetry.instrumentation.requests", "RequestsInstrumentor"),
    )
    import importlib

    for module_path, class_name in candidates:
        try:
            module = importlib.import_module(module_path)
            instrumentor = getattr(module, class_name)()
            if not instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.instrument()
            done.append(class_name)
        except Exception:  # pragma: no cover - package absent or double-instrument
            continue
    return done


def install_otel(settings: ObsSettings) -> OtelProviders | None:
    """Install real SDK providers + OTLP/HTTP exporters. Returns None if unwired.

    Wired only when the SDK is importable **and** an ``otlp_endpoint`` resolves;
    otherwise the caller keeps the no-op/in-memory path. Never raises — an
    exporter/instrumentation failure degrades to whatever was installed so far.
    """
    if not (_OTEL_SDK and settings.enabled and settings.otlp_endpoint):
        return None

    base = settings.otlp_endpoint
    resource = Resource.create(
        {
            "service.name": settings.service_name,
            "service.version": settings.version,
            "deployment.environment": settings.environment,
        }
    )
    providers = OtelProviders()

    try:
        sampler = ParentBased(TraceIdRatioBased(settings.traces_sampler_ratio))
        tracer_provider = TracerProvider(resource=resource, sampler=sampler)
        tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=_signal_url(base, "traces")))
        )
        _trace.set_tracer_provider(tracer_provider)
        providers.tracer_provider = tracer_provider
        # W3C trace-context propagation across api -> Celery worker -> mcp-gateway.
        set_global_textmap(TraceContextTextMapPropagator())
    except Exception:  # pragma: no cover - defensive
        _log.exception("failed to install OTel tracer provider")

    try:
        # Long interval: metric truth is the internal RecordingMetrics scrape;
        # this reader carries framework/SDK metrics. Kept from hammering a dead
        # endpoint in tests — a real deployment tunes it via the collector.
        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=_signal_url(base, "metrics")),
            export_interval_millis=30_000,
        )
        meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
        from opentelemetry import metrics as _metrics

        _metrics.set_meter_provider(meter_provider)
        providers.meter_provider = meter_provider
    except Exception:  # pragma: no cover - defensive
        _log.exception("failed to install OTel meter provider")

    try:
        logger_provider = LoggerProvider(resource=resource)
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(endpoint=_signal_url(base, "logs")))
        )
        _set_logger_provider(logger_provider)
        providers.logger_provider = logger_provider
    except Exception:  # pragma: no cover - defensive
        _log.exception("failed to install OTel logger provider")

    try:
        providers.instrumented = _instrument_frameworks()
    except Exception:  # pragma: no cover - defensive
        _log.exception("failed to install OTel auto-instrumentation")

    return providers


def shutdown_otel(providers: OtelProviders | None) -> None:
    """Flush + close the installed providers (best effort; never raises)."""
    if providers is None:
        return
    for handle in (providers.tracer_provider, providers.meter_provider, providers.logger_provider):
        shutdown = getattr(handle, "shutdown", None)
        if callable(shutdown):
            try:
                shutdown()
            except Exception:  # pragma: no cover - defensive
                _log.warning("OTel provider shutdown failed", exc_info=True)
