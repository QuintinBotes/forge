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

from forge_integrations.pm.asana.adapter import AsanaAdapter
from forge_integrations.pm.clickup.adapter import ClickUpAdapter
from forge_integrations.pm.errors import (
    ExternalNotFound,
    MappingError,
    PMAuthError,
    PMError,
    ProviderError,
    RateLimitError,
    SyncConflict,
    WebhookVerificationError,
)
from forge_integrations.pm.generic.adapter import GenericAdapter
from forge_integrations.pm.github_projects.adapter import GitHubProjectsAdapter
from forge_integrations.pm.gitlab.adapter import GitLabAdapter
from forge_integrations.pm.hashing import external_content_hash, forge_content_hash
from forge_integrations.pm.jira.adapter import JiraAdapter
from forge_integrations.pm.linear.adapter import LinearAdapter
from forge_integrations.pm.monday.adapter import MondayAdapter
from forge_integrations.pm.registry import build_adapter
from forge_integrations.pm.sync_engine import (
    AuditSink,
    BoardWriter,
    ForgeTaskPatch,
    InMemoryAuditSink,
    InMemoryBoardWriter,
    InMemoryLinkRepository,
    LinkRecord,
    LinkRepository,
    PMSyncEngine,
)
from forge_integrations.pm.transport import FixturePMTransport, HttpResponse
from forge_integrations.pm.trello.adapter import TrelloAdapter

__all__ = [
    "AsanaAdapter",
    "AuditSink",
    "BoardWriter",
    "ClickUpAdapter",
    "ExternalNotFound",
    "FixturePMTransport",
    "ForgeTaskPatch",
    "GenericAdapter",
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
    "SyncConflict",
    "TrelloAdapter",
    "WebhookVerificationError",
    "build_adapter",
    "external_content_hash",
    "forge_content_hash",
]
