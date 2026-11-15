"""External PM-adapter SDK (Jira, Linear, Asana, Monday, GitHub Projects,
ClickUp, Trello, GitLab, and a config-driven generic/BYO-board connector).

Public surface:

* :func:`build_adapter` — the OSS extension point (provider -> ``PMAdapter``).
* :class:`PMSyncEngine` — provider-agnostic bidirectional sync.
* :class:`FixturePMTransport` — offline transport for tests (no sockets).
* hashing + error types + the concrete provider adapters.

Importing this package opens **no** network connections.
"""

from __future__ import annotations

from scf_integrations.pm.asana.adapter import AsanaAdapter
from scf_integrations.pm.clickup.adapter import ClickUpAdapter
from scf_integrations.pm.errors import (
    ExternalNotFound,
    MappingError,
    PMAuthError,
    PMError,
    ProviderError,
    RateLimitError,
    SyncConflict,
    WebhookVerificationError,
)
from scf_integrations.pm.generic.adapter import GenericAdapter
from scf_integrations.pm.github_issues.adapter import GitHubIssuesAdapter
from scf_integrations.pm.github_projects.adapter import GitHubProjectsAdapter
from scf_integrations.pm.gitlab.adapter import GitLabAdapter
from scf_integrations.pm.hashing import external_content_hash, scf_content_hash
from scf_integrations.pm.jira.adapter import JiraAdapter
from scf_integrations.pm.linear.adapter import LinearAdapter
from scf_integrations.pm.monday.adapter import MondayAdapter
from scf_integrations.pm.registry import build_adapter
from scf_integrations.pm.sync_engine import (
    AuditSink,
    BoardWriter,
    InMemoryAuditSink,
    InMemoryBoardWriter,
    InMemoryLinkRepository,
    LinkRecord,
    LinkRepository,
    PMSyncEngine,
    ScfTaskPatch,
)
from scf_integrations.pm.transport import FixturePMTransport, HttpResponse
from scf_integrations.pm.trello.adapter import TrelloAdapter

__all__ = [
    "AsanaAdapter",
    "AuditSink",
    "BoardWriter",
    "ClickUpAdapter",
    "ExternalNotFound",
    "FixturePMTransport",
    "GenericAdapter",
    "GitHubIssuesAdapter",
    "GitHubProjectsAdapter",
    "GitLabAdapter",
    "HttpResponse",
    "InMemoryAuditSink",
    "InMemoryBoardWriter",
    "InMemoryLinkRepository",
    "JiraAdapter",
    "LinearAdapter",
    "LinkRecord",
    "LinkRepository",
    "MappingError",
    "MondayAdapter",
    "PMAuthError",
    "PMError",
    "PMSyncEngine",
    "ProviderError",
    "RateLimitError",
    "ScfTaskPatch",
    "SyncConflict",
    "TrelloAdapter",
    "WebhookVerificationError",
    "build_adapter",
    "external_content_hash",
    "scf_content_hash",
]
