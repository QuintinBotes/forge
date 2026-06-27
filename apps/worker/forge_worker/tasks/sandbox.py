"""Sandbox reaper task + worker-boot reap hook (F19 AC12).

Removes orphaned ``forge.sandbox=true`` containers left by worker crashes. Runs on
a Celery beat (``sandbox.reap_orphans``) and once on worker boot (``worker_ready``).

The agent-runtime ``reap_orphans`` is ``forge_db``-free; this module — which owns the
``forge_db`` session — resolves which terminal runs still have a live sandbox row and
passes those ids in. Everything is defensive: a missing daemon/DB degrades to a
no-op rather than crashing the worker.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from forge_agent.sandbox import SandboxSettings, build_sandbox_provider, reap_orphans
from forge_contracts import SandboxKind
from forge_db.models import AgentRun, SandboxInstance
from forge_db.models.enums import RunStatus, SandboxStatus
from forge_db.session import create_session_factory
from forge_worker.celery_app import celery_app

__all__ = [
    "reap_on_worker_ready",
    "reap_orphans_task",
    "run_reap_pass",
    "terminal_run_ids_with_live_sandbox",
]

_TERMINAL_STATUSES = (
    RunStatus.SUCCEEDED,
    RunStatus.FAILED,
    RunStatus.ESCALATED,
    RunStatus.CANCELLED,
)


def terminal_run_ids_with_live_sandbox(session: Session) -> set[str]:
    """Agent-run ids whose run is terminal but whose sandbox row is not yet removed."""
    stmt = (
        select(SandboxInstance.agent_run_id)
        .join(AgentRun, AgentRun.id == SandboxInstance.agent_run_id)
        .where(AgentRun.status.in_(_TERMINAL_STATUSES))
        .where(SandboxInstance.status != SandboxStatus.REMOVED)
    )
    return {str(rid) for rid in session.execute(stmt).scalars().all()}


def _collect_terminal_ids(
    settings: SandboxSettings,
    session_factory: sessionmaker[Session] | None,
) -> set[str]:
    if settings.kind is not SandboxKind.CONTAINER:
        return set()  # the worktree provider never orphans a container; skip the DB
    factory = session_factory or create_session_factory()
    try:
        with factory() as session:
            return terminal_run_ids_with_live_sandbox(session)
    except Exception:  # DB unavailable -> reap on TTL/exit heuristics only
        return set()


def run_reap_pass(
    *,
    provider: Any = None,
    settings: SandboxSettings | None = None,
    session_factory: sessionmaker[Session] | None = None,
) -> dict[str, Any]:
    """Run one reap pass; returns ``{removed, kind}``. Injectable for tests."""
    resolved = settings or SandboxSettings.from_env()
    prov = provider or build_sandbox_provider(resolved)
    terminal_ids = _collect_terminal_ids(resolved, session_factory)
    removed = asyncio.run(reap_orphans(prov, terminal_run_ids=terminal_ids))
    return {"removed": removed, "kind": resolved.kind.value}


@celery_app.task(name="sandbox.reap_orphans")
def reap_orphans_task() -> dict[str, Any]:
    """Beat task: reap orphaned sandbox containers."""
    return run_reap_pass()


def reap_on_worker_ready(**_kwargs: Any) -> None:
    """``worker_ready`` hook: one reap pass on boot (best-effort)."""
    with contextlib.suppress(Exception):
        run_reap_pass()


def _connect_worker_ready() -> Callable[..., None]:
    from celery.signals import worker_ready

    worker_ready.connect(reap_on_worker_ready, weak=False)
    return reap_on_worker_ready


_connect_worker_ready()
