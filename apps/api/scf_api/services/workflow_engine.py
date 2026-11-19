"""Workflow-engine selection (F25 AC1).

Both engines implement the **frozen** :class:`scf_contracts.WorkflowEngine`
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

import yaml

from scf_workflow import WorkflowEngineImpl
from scf_workflow.editor.repository import DbWorkflowDefinitionRepository
from scf_workflow.editor.store import DbWorkflowDefinitionStore, ResolvingDefinitionProvider
from scf_workflow.temporal.client import get_temporal_client
from scf_workflow.temporal.config import TemporalSettings
from scf_workflow.temporal.engine import TemporalWorkflowEngine

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def resolve_backend(settings: TemporalSettings | None = None) -> str:
    """Return the configured workflow engine backend (env-driven)."""
    return (settings or TemporalSettings()).workflow_engine_backend


def build_definition_provider(session: Session) -> ResolvingDefinitionProvider:
    """Resolve a definition name to the workspace's published revision over the bundled one."""
    return ResolvingDefinitionProvider(
        DbWorkflowDefinitionStore(DbWorkflowDefinitionRepository(session))
    )


def published_revision_is_line(session: Session, *, workspace_id: uuid.UUID, name: str) -> bool:
    """True when the workspace's published revision of ``name`` says ``dsl_version: "2"``.

    Read off the raw YAML, so a line this release cannot parse still reads as a line.
    """
    repo = DbWorkflowDefinitionRepository(session)
    definition = repo.get(workspace_id, name)
    if definition is None or definition.current_published_revision_id is None:
        return False
    revision = repo.get_revision_by_id(definition.current_published_revision_id)
    if revision is None:
        return False
    try:
        raw = yaml.safe_load(revision.dsl_yaml)
    except yaml.YAMLError:
        return False
    return isinstance(raw, dict) and raw.get("dsl_version") == "2"


def active_line_runs(
    session: Session, *, workspace_id: uuid.UUID, name: str, task_id: uuid.UUID
) -> int:
    """The workspace's unfinished runs of the line ``name``, counted under a row lock.

    The lock is on the definition row (spec K §4.11), so two starts of one line
    count one at a time; the caller's commit of its new run releases it. The task's
    own run is not counted: a start either reuses it (one that never started) or
    is refused as a duplicate.
    """
    from sqlalchemy import func, select

    from scf_db.models.enums import RunStatus
    from scf_db.models.runs import WorkflowRun as DbWorkflowRun
    from scf_db.models.workflow_editor import WorkflowDefinition as DbDefinition

    session.execute(
        select(DbDefinition.id)
        .where(DbDefinition.workspace_id == workspace_id, DbDefinition.name == name)
        .with_for_update()
    )
    finished = (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED)
    count = session.scalar(
        select(func.count())
        .select_from(DbWorkflowRun)
        .where(
            DbWorkflowRun.workspace_id == workspace_id,
            DbWorkflowRun.workflow_name == name,
            DbWorkflowRun.status.notin_(finished),
            DbWorkflowRun.task_id != task_id,
        )
    )
    return count or 0


def revision_text(session: Session, revision_id: uuid.UUID) -> str:
    """The DSL text of a definition revision (a line run carries it, spec K §4.2)."""
    revision = DbWorkflowDefinitionRepository(session).get_revision_by_id(revision_id)
    if revision is None:  # admission just resolved it
        raise LookupError(f"no workflow definition revision {revision_id}")
    return revision.dsl_yaml


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

    The V1 engine also gets the definition provider, so a run started with an
    explicit definition pins its revision and transitions on it (spec K §4.2).
    The Temporal engine takes no provider: ``FeatureWorkflow`` runs only
    ``default_feature``, and a line run carries its revision's text. It does take
    ``session.commit``: it commits the run row before starting the workflow (spec K
    §4.11).
    """
    from scf_workflow.store import SqlAlchemyWorkflowStore

    settings = settings or TemporalSettings()
    store = SqlAlchemyWorkflowStore(session, workspace_id=workspace_id)
    if settings.workflow_engine_backend != "temporal":
        return WorkflowEngineImpl(
            store=store, definition_provider=build_definition_provider(session)
        )
    return TemporalWorkflowEngine(
        workspace_id=workspace_id,
        store=store,
        client_factory=lambda: get_temporal_client(settings),
        task_queue=settings.temporal_task_queue,
        commit=session.commit,
        checks_timeout_seconds=settings.checks_activity_timeout,
    )


__all__ = [
    "active_line_runs",
    "build_definition_provider",
    "published_revision_is_line",
    "resolve_backend",
    "revision_text",
    "select_workflow_engine",
]
