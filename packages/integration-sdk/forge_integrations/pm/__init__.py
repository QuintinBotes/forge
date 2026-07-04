"""F18 external PM-adapter SDK (Jira, Linear).

Public surface:

* :func:`build_adapter` — the OSS extension point (provider -> ``PMAdapter``).
* :class:`PMSyncEngine` — provider-agnostic bidirectional sync.
* :class:`FixturePMTransport` — offline transport for tests (no sockets).
* hashing + error types + the concrete ``JiraAdapter`` / ``LinearAdapter``.

Importing this package opens **no** network connections.
"""

from __future__ import annotations

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
from forge_integrations.pm.hashing import external_content_hash, forge_content_hash
from forge_integrations.pm.jira.adapter import JiraAdapter
from forge_integrations.pm.linear.adapter import LinearAdapter
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

__all__ = [
    "AuditSink",
    "BoardWriter",
    "ExternalNotFound",
    "FixturePMTransport",
    "ForgeTaskPatch",
    "HttpResponse",
    "InMemoryAuditSink",
    "InMemoryBoardWriter",
    "InMemoryLinkRepository",
    "JiraAdapter",
    "LinearAdapter",
    "LinkRecord",
    "LinkRepository",
    "MappingError",
    "PMAuthError",
    "PMError",
    "PMSyncEngine",
    "ProviderError",
    "RateLimitError",
    "SyncConflict",
    "WebhookVerificationError",
    "build_adapter",
    "external_content_hash",
    "forge_content_hash",
]
