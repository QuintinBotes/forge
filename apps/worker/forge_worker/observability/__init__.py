"""Worker-side observability wiring (HARD-10 §3.3).

One telemetry init per Celery worker process (real OTLP export when enabled, a
no-op otherwise) plus the single :class:`~forge_obs.cost.meter.UsageMeter` the
agent runner's model / embedding / rerank calls emit cost through — one durable
emission point over the ``cost_event`` ledger.
"""

from __future__ import annotations

from forge_worker.observability.init import (
    build_usage_meter,
    setup_worker_telemetry,
)

__all__ = ["build_usage_meter", "setup_worker_telemetry"]
