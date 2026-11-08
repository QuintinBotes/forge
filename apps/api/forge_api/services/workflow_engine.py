"""Workflow-engine selection (F25 AC1).

Both engines implement the **frozen** :class:`forge_contracts.WorkflowEngine`
protocol, so the ``/workflow/*`` router is engine-agnostic. ``WORKFLOW_ENGINE_BACKEND``
selects which one is wired:

* ``postgres_fsm`` (default) → F07's FSM (``WorkflowEngineImpl``) over the Postgres run store.
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
    session: Session,
    workspace_id: uuid.UUID,
    settings: TemporalSettings | None = None,
) -> WorkflowEngineImpl | TemporalWorkflowEngine:
    """Build the deployment's engine over ``workspace_id``'s runs in Postgres.

    Both engines read and write ``workflow_run`` through a workspace-scoped
    ``SqlAlchemyWorkflowStore``, so runs survive restarts, the worker's
    projection writes find their row, and a foreign run reads as missing
    (spec K §4.3 prerequisites). The caller commits.
    """
    from forge_workflow.store import SqlAlchemyWorkflowStore

    settings = settings or TemporalSettings()
    store = SqlAlchemyWorkflowStore(session, workspace_id=workspace_id)
    if settings.workflow_engine_backend != "temporal":
        return WorkflowEngineImpl(store=store)
    return TemporalWorkflowEngine(
        workspace_id=workspace_id,
        store=store,
        client_factory=lambda: get_temporal_client(settings),
        task_queue=settings.temporal_task_queue,
    )


__all__ = ["resolve_backend", "select_workflow_engine"]
