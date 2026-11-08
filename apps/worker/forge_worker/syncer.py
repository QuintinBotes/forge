"""Knowledge syncer task (plan Task 1.4 — full + incremental sync, background half).

Drives a knowledge source's full or incremental (git-diff) sync into the hybrid
retrieval store. As with the indexer, the logic is split so it is unit-testable
without Celery or a live database:

* :func:`sync_files` — pure: full-sync an in-memory ``{path: content}`` mapping
  into any :class:`~forge_knowledge.sync.SyncStore` (idempotent by content hash).
* :func:`sync_repo` — pure: full / incremental sync from a checked-out repo root.
* :func:`sync_tracker_source` — a GitHub tracker source (spec J §2.3) through a
  client from :func:`github_tracker_client`: the workspace's GitHub Issues
  connection, never a credential of its own.
* :func:`sync_source_task` — the thin Celery task that builds the default service
  from configuration and delegates to the pure helpers.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from forge_contracts import IndexResult
from forge_contracts.enums import SyncMode
from forge_db.models import KnowledgeSource
from forge_db.models.enums import PMConnectionStatus, PMProvider
from forge_integrations.github import DEFAULT_BASE_URL, GitHubClient, RetryPolicy
from forge_knowledge import KnowledgeService, full_sync, sync_source
from forge_knowledge.sync import (
    PageClient,
    commit_limit,
    is_tracker_source,
    sync_tracker,
    tracker_repo,
)
from forge_worker.celery_app import celery_app
from forge_worker.indexer import build_knowledge_service
from forge_worker.reliability import ForgeTask

if TYPE_CHECKING:
    from forge_api.services.pm_service import PMConnectionService

__all__ = [
    "TRACKER_NOT_CONNECTED",
    "TrackerNotConnectedError",
    "github_tracker_client",
    "sync_files",
    "sync_repo",
    "sync_source_task",
    "sync_tracker_source",
]

#: Error code of a tracker sync whose workspace has no GitHub Issues connection.
TRACKER_NOT_CONNECTED = "tracker_not_connected"

#: Builds the paginated GitHub reader for one tracker source.
TrackerClientFactory = Callable[[KnowledgeSource], PageClient]


class TrackerNotConnectedError(LookupError):
    """No connected GitHub Issues integration covers a tracker source's repository."""

    code = TRACKER_NOT_CONNECTED


def sync_files(
    store: KnowledgeService,
    source_id: str,
    files: Mapping[str, str],
    *,
    prune: bool = True,
) -> IndexResult:
    """Full-sync an in-memory ``{path: content}`` mapping into ``store``."""
    return full_sync(store, source_id, files, prune=prune)


def sync_repo(
    store: KnowledgeService,
    source_id: str,
    root: str,
    *,
    mode: SyncMode = SyncMode.FULL,
    base_ref: str | None = None,
    head_ref: str | None = None,
    prune: bool = True,
    max_commits: int | None = None,
) -> IndexResult:
    """Full / incremental (git-diff) sync from a checked-out repository ``root``."""
    return sync_source(
        store,
        source_id,
        root,
        mode=mode,
        base_ref=base_ref,
        head_ref=head_ref,
        prune=prune,
        max_commits=max_commits,
    )


def github_tracker_client(
    source: KnowledgeSource, connections: PMConnectionService
) -> GitHubClient:
    """A :class:`GitHubClient` for ``source``'s repository from the workspace's connection.

    Built as the GitHub Issues adapter builds its client: the connection's
    base URL (default ``api.github.com``), ``RetryPolicy()``, and the
    connection's vault credential as the bearer token, read through
    ``PMConnectionService``'s own lookup (no credential of its own). Raises
    :class:`TrackerNotConnectedError` when no enabled GitHub Issues
    connection with a credential covers the repository.
    """
    repo = tracker_repo(source.uri).lower()
    for connection in connections.list(source.workspace_id):
        if (
            connection.provider != PMProvider.GITHUB_ISSUES
            or connection.status == PMConnectionStatus.DISABLED
            or GitHubClient.owner_repo(connection.external_project_key).lower() != repo
        ):
            continue
        header = connections._auth_header(connection)  # the adapter's "Bearer <secret>"
        if header and header.startswith("Bearer "):
            return GitHubClient(
                token=header.removeprefix("Bearer "),
                base_url=connection.external_base_url or DEFAULT_BASE_URL,
                retry=RetryPolicy(),
            )
    raise TrackerNotConnectedError(
        f"{TRACKER_NOT_CONNECTED}: no connected GitHub Issues integration for {repo}"
    )


def sync_tracker_source(
    store: KnowledgeService, source: KnowledgeSource, client_factory: TrackerClientFactory
) -> IndexResult:
    """Full sync of a GitHub tracker source through a client from ``client_factory``."""
    client = client_factory(source)
    try:
        return sync_tracker(store, source, client)
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


def _github_tracker_client(source: KnowledgeSource) -> GitHubClient:  # pragma: no cover
    """Production seam: the PM connection service the GitHub Issues dispatch path uses."""
    from forge_worker.tasks.pm_dispatch import _connection_service, _session_factory

    return github_tracker_client(source, _connection_service(_session_factory()))


def _load_source(store: KnowledgeService, source_id: str) -> KnowledgeSource | None:
    with store.session_factory() as session:
        return session.get(KnowledgeSource, uuid.UUID(source_id))


#: The tracker client factory the task uses (tests swap in a fake).
tracker_client_factory: TrackerClientFactory = _github_tracker_client


@celery_app.task(bind=True, base=ForgeTask, name="forge.knowledge.sync_source")
def sync_source_task(
    self: ForgeTask,
    source_id: str,
    *,
    files: dict[str, str] | None = None,
    root: str | None = None,
    mode: str = SyncMode.FULL.value,
    base_ref: str | None = None,
    head_ref: str | None = None,
    prune: bool = True,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Celery entrypoint: full / incremental sync for ``source_id``.

    Provide either ``files`` (full sync of inline content) or ``root`` (sync a
    checked-out tree; incremental requires ``base_ref``; commit history only
    when the source's ``config.commits`` enables it). A GitHub tracker source
    needs neither: it syncs through the workspace's GitHub Issues connection
    (``POST /knowledge/sync`` enqueues it). The optional ``idempotency_key``
    collapses a re-delivered enqueue to a single run.
    """
    if self.is_duplicate(idempotency_key):
        return {"deduplicated": True, "idempotency_key": idempotency_key}
    service = build_knowledge_service()
    sync_mode = SyncMode(mode)
    if files is not None:
        result = sync_files(service, source_id, files, prune=prune)
    elif root is not None:
        source = _load_source(service, source_id)
        result = sync_repo(
            service,
            source_id,
            root,
            mode=sync_mode,
            base_ref=base_ref,
            head_ref=head_ref,
            prune=prune,
            max_commits=commit_limit(source.config) if source is not None else None,
        )
    elif (source := _load_source(service, source_id)) is not None and is_tracker_source(source):
        result = sync_tracker_source(service, source, tracker_client_factory)
    else:
        raise ValueError("sync_source_task requires either 'files' or 'root'")
    return result.model_dump()
