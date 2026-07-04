"""GitHub App and Slack integration clients built against frozen interfaces.

Plan Task 1.13. All clients are fixture-backed (``httpx.MockTransport`` in
tests) and make no live external calls. Public surface:

- :class:`GitHubClient` — repo sync, open PR, request reviews, CI webhook parse.
- :class:`SlackNotifier` — task-status + approval notifications.
- :class:`BasePMAdapter` / :class:`GenericPMAdapter` — external PM adapter surface.
- webhook helpers — :func:`parse_github_webhook`, signature verification.
"""

from __future__ import annotations

from .errors import GitHubError, IntegrationError, SlackError
from .github import GitHubClient
from .pm_adapter import BasePMAdapter, GenericPMAdapter
from .slack import SlackNotifier
from .webhooks import (
    parse_github_webhook,
    sign_github_payload,
    verify_github_signature,
    verify_slack_signature,
)

__version__ = "0.1.0"

__all__ = [
    "BasePMAdapter",
    "GenericPMAdapter",
    "GitHubClient",
    "GitHubError",
    "IntegrationError",
    "SlackError",
    "SlackNotifier",
    "parse_github_webhook",
    "sign_github_payload",
    "verify_github_signature",
    "verify_slack_signature",
]
