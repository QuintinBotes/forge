"""Typed errors raised by the integration clients."""

from __future__ import annotations

from forge_contracts import ForgeError


class IntegrationError(ForgeError):
    """Base class for outbound integration failures."""


class GitHubError(IntegrationError):
    """A GitHub API call returned an error response.

    Carries the HTTP status and (best-effort) the provider message so callers
    can branch without re-parsing the response body.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        self.status_code = status_code
        prefix = f"GitHub API error {status_code}: " if status_code is not None else ""
        super().__init__(f"{prefix}{message}")


class SlackError(IntegrationError):
    """A Slack API call failed at the transport level."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        self.status_code = status_code
        prefix = f"Slack API error {status_code}: " if status_code is not None else ""
        super().__init__(f"{prefix}{message}")


__all__ = ["GitHubError", "IntegrationError", "SlackError"]
