"""Typed errors for the PM-adapter surface (F18)."""

from __future__ import annotations

from forge_integrations.errors import IntegrationError


class PMError(IntegrationError):
    """Base for all PM-adapter errors."""


class PMAuthError(PMError):
    """Authentication/authorization failure talking to the provider."""


class WebhookVerificationError(PMError):
    """Inbound webhook signature/secret verification failed."""


class ExternalNotFound(PMError):
    """The referenced external issue does not exist."""


class MappingError(PMError):
    """A status/priority/field value could not be mapped (never silently dropped)."""


class SyncConflict(PMError):
    """A manual conflict blocks an automatic write."""

    def __init__(self, message: str, *, link_id: str | None = None) -> None:
        super().__init__(message)
        self.link_id = link_id


class RateLimitError(PMError):
    """Provider rate limit hit; carries an optional retry-after hint (seconds)."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ProviderError(PMError):
    """An unexpected upstream provider failure (maps to HTTP 502)."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


__all__ = [
    "ExternalNotFound",
    "MappingError",
    "PMAuthError",
    "PMError",
    "ProviderError",
    "RateLimitError",
    "SyncConflict",
    "WebhookVerificationError",
]
