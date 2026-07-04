"""Knowledge syncer task (plan Task 1.4 — full + incremental sync, background half).

Drives a knowledge source's full or incremental (git-diff) sync into the hybrid
retrieval store. As with the indexer, the logic is split so it is unit-testable
without Celery or a live database:

* :func:`sync_files` — pure: full-sync an in-memory ``{path: content}`` mapping
  into any :class:`~forge_knowledge.sync.SyncStore` (idempotent by content hash).
* :func:`sync_repo` — pure: full / incremental sync from a checked-out repo root.
* :func:`sync_source_task` — the thin Celery task that builds the default service
  from configuration and delegates to the pure helpers.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from forge_contracts import IndexResult
from forge_contracts.enums import SyncMode
from forge_knowledge import KnowledgeService, full_sync, sync_source
from forge_worker.celery_app import celery_app
from forge_worker.indexer import build_knowledge_service
from forge_worker.reliability import ForgeTask

__all__ = [
    "sync_files",
    "sync_repo",
    "sync_source_task",
]


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
    )


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
    checked-out tree; incremental requires ``base_ref``). The optional
    ``idempotency_key`` collapses a re-delivered enqueue to a single run.
    """
    if self.is_duplicate(idempotency_key):
        return {"deduplicated": True, "idempotency_key": idempotency_key}
    service = build_knowledge_service()
    sync_mode = SyncMode(mode)
    if files is not None:
        result = sync_files(service, source_id, files, prune=prune)
    elif root is not None:
        result = sync_repo(
            service,
            source_id,
            root,
            mode=sync_mode,
            base_ref=base_ref,
            head_ref=head_ref,
            prune=prune,
        )
    else:
        raise ValueError("sync_source_task requires either 'files' or 'root'")
    return result.model_dump()
