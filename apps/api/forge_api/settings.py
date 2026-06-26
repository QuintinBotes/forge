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

    # CORS — list of allowed origins (JSON-encoded in the env var).
    cors_origins: list[str] = ["*"]
    cors_allow_credentials: bool = True

    docs_enabled: bool = True


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide cached :class:`Settings` instance."""
    return Settings()
