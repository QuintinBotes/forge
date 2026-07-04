"""Environment-driven observability settings (F38 §4).

``ObsSettings`` is a frozen dataclass so a settings object can be shared across
threads and used as part of the idempotency key of :func:`~forge_obs.telemetry.
setup_telemetry`. ``from_env`` reads the deploy-documented ``OBS_*`` / ``OTEL_*``
variables; everything defaults to the lean stack (observability **off**) so the
default boot path never attempts an export (spec AC18).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY


@dataclass(frozen=True)
class ObsSettings:
    """Process-wide observability configuration (spec §4, frozen contract)."""

    enabled: bool = False
    service_name: str = "forge"
    version: str = "0.0.0"
    environment: str = "dev"
    otlp_endpoint: str | None = None  # None -> no OTLP export is attempted
    traces_sampler_ratio: float = 0.1
    metric_workspace_label: bool = False  # cardinality guard (spec AC11)
    prometheus_scrape_enabled: bool = True

    @classmethod
    def from_env(cls, service_name: str = "forge") -> ObsSettings:
        """Build settings from the ``OBS_*`` / ``OTEL_*`` environment contract."""
        ratio_raw = os.environ.get("OTEL_TRACES_SAMPLER_ARG", "0.1")
        try:
            ratio = float(ratio_raw)
        except ValueError:
            ratio = 0.1
        return cls(
            enabled=_env_bool("OBS_ENABLED", default=False),
            service_name=os.environ.get("OTEL_SERVICE_NAME", service_name),
            version=os.environ.get("FORGE_VERSION", "0.0.0"),
            environment=os.environ.get("FORGE_ENVIRONMENT", "dev"),
            otlp_endpoint=os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or None,
            traces_sampler_ratio=ratio,
            metric_workspace_label=_env_bool("OBS_METRIC_WORKSPACE_LABEL", default=False),
            prometheus_scrape_enabled=_env_bool("OBS_PROMETHEUS_SCRAPE", default=True),
        )


__all__ = ["ObsSettings"]
