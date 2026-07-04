"""GitHub App and Slack integration clients built against frozen interfaces.

Plan Task 1.13. All clients are fixture-backed (``httpx.MockTransport`` in
tests) and make no live external calls. Public surface:

- :class:`GitHubClient` — repo sync, open PR, request reviews, CI webhook parse.
- :class:`SlackNotifier` — task-status + approval notifications.
- :class:`BasePMAdapter` / :class:`GenericPMAdapter` — external PM adapter surface.
- webhook helpers — :func:`parse_github_webhook`, signature verification.
"""

from __future__ import annotations

from .audit import AuditSink, GitHubAuditEvent
from .errors import GitHubError, IntegrationError, SlackError
from .github import GitHubClient, RetryPolicy, Review, ReviewComment
from .github_auth import (
    InstallationTokenProvider,
    build_app_jwt,
    load_private_key,
)
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
    "AuditSink",
    "BasePMAdapter",
    "GenericPMAdapter",
    "GitHubAuditEvent",
    "GitHubClient",
    "GitHubError",
    "InstallationTokenProvider",
    "IntegrationError",
    "RetryPolicy",
    "Review",
    "ReviewComment",
    "SlackError",
    "SlackNotifier",
    "build_app_jwt",
    "load_private_key",
    "parse_github_webhook",
    "sign_github_payload",
    "verify_github_signature",
    "verify_slack_signature",
]
