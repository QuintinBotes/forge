"""Orphan sandbox reaping (F19 AC12).

A worker crash can leave a ``forge.sandbox=true`` container behind. The reaper
removes any such container whose run is terminal, whose container has exited, or
which is older than ``FORGE_SANDBOX_MAX_TTL_SECONDS``. It runs on a Celery beat and
once on worker boot (``worker_ready``).

DB access (resolving which runs are terminal) belongs to the worker, which owns the
``forge_db`` session; this module stays ``forge_db``-free and takes the resolved
``terminal_run_ids`` so the agent-runtime package keeps its narrow dependency set.
"""

from __future__ import annotations

from collections.abc import Iterable

from forge_contracts import SandboxKind, SandboxProvider


async def reap_orphans(
    provider: SandboxProvider,
    *,
    terminal_run_ids: Iterable[str] | None = None,
) -> int:
    """Remove orphaned sandbox containers; return the count removed.

    For the worktree (local) provider this is always 0 (host subprocesses cannot
    orphan a container). For the container provider, terminal runs are also reaped.
    """
    ids = {str(rid) for rid in (terminal_run_ids or [])}
    if getattr(provider, "kind", None) is SandboxKind.CONTAINER:
        return await provider.reap_orphans(terminal_run_ids=ids)  # type: ignore[call-arg]
    return await provider.reap_orphans()


__all__ = ["reap_orphans"]
