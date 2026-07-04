"""One shared telemetry init every Forge app calls at startup (F38 §4).

``setup_telemetry(service_name, settings)`` is **idempotent** and never raises:

- ``settings.enabled`` False (the lean default) or no export surface at all ->
  the no-op providers are installed (``get_metrics()`` returns ``NoopMetrics``)
  and no export of any kind is attempted (spec AC1/AC18).
- enabled -> the in-process :class:`~forge_obs.metrics.RecordingMetrics`
  registry is installed and backs the internal Prometheus scrape surface. When
  an ``otlp_endpoint`` also resolves and the OpenTelemetry SDK is importable,
  :func:`~forge_obs.otel_export.install_otel` additionally stands up the real
  ``TracerProvider``/``MeterProvider``/``LoggerProvider`` with OTLP/HTTP
  exporters + W3C propagation + best-effort auto-instrumentation (FastAPI/
  SQLAlchemy/httpx/Celery/Redis) behind this same handle — no caller changes.
  With ``enabled`` but no endpoint (or no SDK), only the in-process recorder is
  installed and no export of any kind is attempted.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from forge_obs.logging import configure_logging
from forge_obs.metrics import (
    ForgeMetrics,
    NoopMetrics,
    RecordingMetrics,
    set_metrics,
)
from forge_obs.otel_export import OtelProviders, install_otel, shutdown_otel
from forge_obs.settings import ObsSettings

__all__ = ["Telemetry", "setup_telemetry", "shutdown_telemetry"]


@dataclass
class Telemetry:
    """Handle returned by :func:`setup_telemetry`."""

    service_name: str
    settings: ObsSettings
    metrics: ForgeMetrics
    enabled: bool = False
    otel: OtelProviders | None = None
    _shutdown: bool = field(default=False, repr=False)

    @property
    def exporting(self) -> bool:
        """True when real OTLP providers are installed (not just the recorder)."""
        return self.otel is not None

    def shutdown(self) -> None:
        """Flush + close the real exporters (no-op for the in-process registry)."""
        shutdown_otel(self.otel)
        self.otel = None
        self._shutdown = True


_lock = threading.Lock()
_current: Telemetry | None = None


def setup_telemetry(service_name: str, settings: ObsSettings | None = None) -> Telemetry:
    """Install providers + the process metrics singleton. Safe to call twice.

    A repeated call with the same ``(service_name, settings)`` returns the
    existing handle without re-creating instruments; a differing call swaps the
    providers (tests / explicit reconfiguration).
    """
    global _current
    cfg = settings or ObsSettings.from_env(service_name)
    with _lock:
        if (
            _current is not None
            and _current.service_name == service_name
            and _current.settings == cfg
        ):
            return _current

        # A prior handle for a different config: flush its exporters first.
        if _current is not None:
            _current.shutdown()

        exportable = bool(cfg.otlp_endpoint) or cfg.prometheus_scrape_enabled
        otel: OtelProviders | None = None
        if cfg.enabled and exportable:
            metrics: ForgeMetrics = RecordingMetrics(service=service_name)
            enabled = True
            # Real OTLP export is wired only when an endpoint resolves and the
            # SDK is present; otherwise the recorder alone backs /metrics.
            otel = install_otel(cfg)
        else:
            metrics = NoopMetrics()
            enabled = False
        set_metrics(metrics)
        configure_logging(service_name=service_name, settings=cfg)
        _current = Telemetry(
            service_name=service_name,
            settings=cfg,
            metrics=metrics,
            enabled=enabled,
            otel=otel,
        )
        return _current


def shutdown_telemetry() -> None:
    """Tear down the process telemetry (tests / process exit)."""
    global _current
    with _lock:
        if _current is not None:
            _current.shutdown()
        _current = None
        set_metrics(NoopMetrics())
