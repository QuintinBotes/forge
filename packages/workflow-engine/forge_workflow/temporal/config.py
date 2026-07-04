"""Temporal engine configuration (F25).

Read from the environment with the ``TEMPORAL_`` prefix (plus the shared
``WORKFLOW_ENGINE_BACKEND`` selector) so the API, both workers, and the CLI
resolve one consistent set of settings. Defaults keep the V1 Postgres FSM as the
engine and assume plaintext on the internal Compose network — mTLS + a codec key
are layered on for multi-host / production deployments.
"""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

EngineBackendName = Literal["postgres_fsm", "temporal"]

#: Default Temporal task queue for the feature workflow + its activities.
DEFAULT_TASK_QUEUE = "forge-feature"
#: Default per-deployment Temporal namespace.
DEFAULT_NAMESPACE = "forge"


class TemporalSettings(BaseSettings):
    """Environment-driven Temporal connection + engine-selection settings."""

    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Engine selector (shared with the FSM path). ``temporal`` wires the durable
    # Temporal engine; anything else keeps the in-process Postgres FSM.
    workflow_engine_backend: EngineBackendName = "postgres_fsm"

    temporal_host: str = "temporal:7233"
    temporal_namespace: str = DEFAULT_NAMESPACE
    temporal_task_queue: str = DEFAULT_TASK_QUEUE

    # mTLS to the frontend; empty = plaintext on the internal network only.
    temporal_tls_cert: str | None = None
    temporal_tls_key: str | None = None
    temporal_tls_ca: str | None = None

    # Vault reference resolving the AES key for the RedactingEncryptionCodec.
    # Required (non-empty) when the temporal backend is selected.
    temporal_codec_key: str | None = None

    temporal_workflow_exec_timeout: int = 2_592_000  # 30d
    agent_activity_timeout: int = 7_200  # 2h
    temporal_retention_days: int = 30

    @property
    def is_temporal(self) -> bool:
        """True when this deployment selects the Temporal engine."""
        return self.workflow_engine_backend == "temporal"


__all__ = [
    "DEFAULT_NAMESPACE",
    "DEFAULT_TASK_QUEUE",
    "EngineBackendName",
    "TemporalSettings",
]
