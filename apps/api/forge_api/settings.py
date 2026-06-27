"""Application configuration (Pydantic Settings).

Settings are read from the environment with the ``FORGE_`` prefix so they align
with the rest of the workspace (``FORGE_DATABASE_URL`` is shared with
``forge_db``). ``get_settings`` is cached so the app and its dependencies share a
single resolved instance.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from forge_api import __version__

# Matches ``forge_db.session.DEFAULT_DATABASE_URL`` so the API and the data-model
# layer resolve to the same database when ``FORGE_DATABASE_URL`` is unset.
DEFAULT_DATABASE_URL = "postgresql+psycopg://forge:forge@localhost:5432/forge"
DEFAULT_REDIS_URL = "redis://localhost:6379/0"


class Settings(BaseSettings):
    """Runtime configuration for the Forge API service."""

    model_config = SettingsConfigDict(
        env_prefix="FORGE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "Forge API"
    version: str = __version__
    environment: str = "development"
    debug: bool = False

    # Mounted prefix for every *feature* router. ``/health`` and ``/`` stay at
    # the root so liveness probes are stable regardless of this value.
    api_prefix: str = ""

    database_url: str = DEFAULT_DATABASE_URL
    redis_url: str = DEFAULT_REDIS_URL

    # Filesystem root for the spec engine's SDD artifacts (manifests, plans).
    spec_root: str = "specs"

    # Integration credentials (BYOK). Unset by default so the overnight build
    # never makes live GitHub/Slack calls; configured per workspace in prod.
    github_token: str | None = None
    github_api_url: str = "https://api.github.com"
    # Shared secret GitHub signs webhook deliveries with (``X-Hub-Signature-256``).
    # Unset by default: the webhook ingest route rejects every delivery until a
    # secret is configured (fail-closed — an unsigned webhook is never trusted).
    github_webhook_secret: str | None = None
    slack_token: str | None = None
    slack_default_channel: str | None = None

    # Per-provider alert webhook signing secrets (F17). Unset by default so the
    # corresponding webhook returns 501 Not Configured (fail-closed): an alert is
    # only trusted when its provider secret is configured and the signature over
    # the exact raw bytes verifies.
    pagerduty_webhook_secret: str | None = None
    datadog_webhook_secret: str | None = None
    sentry_webhook_secret: str | None = None
    grafana_webhook_secret: str | None = None

    # Recovery-monitoring knobs (F17).
    incident_recovery_window_seconds: int = 300
    incident_recovery_max_windows: int = 6

    # CORS — explicit list of allowed origins (JSON-encoded in the env var).
    # Locked down by default: no cross-origin access until a deployment names its
    # web origin(s). A wildcard ("*") is never combined with credentials (see
    # ``forge_api.main.create_app``) because that is a credential-leak vector.
    cors_origins: list[str] = []
    cors_allow_credentials: bool = True

    docs_enabled: bool = True


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide cached :class:`Settings` instance."""
    return Settings()
