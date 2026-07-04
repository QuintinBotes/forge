"""Workflow-engine selection (F25 AC1).

Both engines implement the **frozen** :class:`forge_contracts.WorkflowEngine`
protocol, so the ``/workflow/*`` router is engine-agnostic. ``WORKFLOW_ENGINE_BACKEND``
selects which one is wired:

* ``postgres_fsm`` (default) → F07's in-process FSM (``WorkflowEngineImpl``).
* ``temporal`` → the V2 durable :class:`TemporalWorkflowEngine`.

The Temporal engine connects lazily (via a ``client_factory``) so selecting it
never opens a connection at import/DI time.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from forge_workflow import WorkflowEngineImpl
from forge_workflow.temporal.client import get_temporal_client
from forge_workflow.temporal.config import TemporalSettings
from forge_workflow.temporal.engine import TemporalWorkflowEngine

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def resolve_backend(settings: TemporalSettings | None = None) -> str:
    """Return the configured workflow engine backend (env-driven)."""
    return (settings or TemporalSettings()).workflow_engine_backend


def select_workflow_engine(
    *,
    session: Session | None = None,
    workspace_id: uuid.UUID | None = None,
    settings: TemporalSettings | None = None,
) -> WorkflowEngineImpl | TemporalWorkflowEngine:
    """Build the engine the deployment selects.

    The FSM engine is process-local (no session needed). The Temporal engine is
    workspace-scoped: it reads/writes the Postgres projection through a
    ``SqlAlchemyWorkflowStore`` when a session is supplied, else an in-memory
    store (used only where the caller has no DB session).
    """
    settings = settings or TemporalSettings()
    if settings.workflow_engine_backend != "temporal":
        return WorkflowEngineImpl()

    store = None
    if session is not None and workspace_id is not None:
        from forge_workflow.store import SqlAlchemyWorkflowStore

        store = SqlAlchemyWorkflowStore(session, workspace_id=workspace_id)

    return TemporalWorkflowEngine(
        workspace_id=workspace_id or uuid.UUID(int=0),
        store=store,
        client_factory=lambda: get_temporal_client(settings),
        task_queue=settings.temporal_task_queue,
    )


__all__ = ["resolve_backend", "select_workflow_engine"]
