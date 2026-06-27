"""Persistence for workflow runs.

The engine reads and writes :class:`~forge_contracts.WorkflowRun` DTOs through a
:class:`WorkflowStore`. Two implementations ship:

* :class:`InMemoryWorkflowStore` — the unit-test / ephemeral backend; stores
  deep copies so a returned DTO can't mutate engine state by reference.
* :class:`SqlAlchemyWorkflowStore` — the Postgres-backed production store
  (spec: "Postgres FSM"), mapping the DTO onto the ``forge_db`` ORM row.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from forge_contracts import ExecutionMode, RunStatus, WorkflowRun
from forge_workflow.exceptions import WorkflowRunNotFoundError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from forge_db.models.runs import WorkflowRun as DbWorkflowRun


@runtime_checkable
class WorkflowStore(Protocol):
    """Durable store for workflow runs."""

    def create(self, run: WorkflowRun) -> WorkflowRun: ...

    def get(self, run_id: uuid.UUID) -> WorkflowRun: ...

    def update(self, run: WorkflowRun) -> WorkflowRun: ...


class InMemoryWorkflowStore:
    """In-memory :class:`WorkflowStore` (deep-copy isolated)."""

    def __init__(self) -> None:
        self._runs: dict[uuid.UUID, WorkflowRun] = {}

    def create(self, run: WorkflowRun) -> WorkflowRun:
        stored = run.model_copy(deep=True)
        if stored.id is None:
            stored.id = uuid.uuid4()
        self._runs[stored.id] = stored
        return stored.model_copy(deep=True)

    def get(self, run_id: uuid.UUID) -> WorkflowRun:
        try:
            return self._runs[run_id].model_copy(deep=True)
        except KeyError as exc:
            raise WorkflowRunNotFoundError(run_id) from exc

    def update(self, run: WorkflowRun) -> WorkflowRun:
        if run.id is None or run.id not in self._runs:
            raise WorkflowRunNotFoundError(run.id)
        self._runs[run.id] = run.model_copy(deep=True)
        return run.model_copy(deep=True)


class SqlAlchemyWorkflowStore:
    """Postgres-backed :class:`WorkflowStore` over the ``forge_db`` ORM."""

    def __init__(self, session: Session, *, workspace_id: uuid.UUID) -> None:
        self._session = session
        self._workspace_id = workspace_id

    @staticmethod
    def _to_dto(model: DbWorkflowRun) -> WorkflowRun:
        # ``forge_db`` defines its own enum classes (same values as the contract
        # enums); convert by value so the DTO carries the contract types.
        return WorkflowRun(
            id=model.id,
            task_id=model.task_id,
            workflow_name=model.workflow_name,
            current_state=str(model.current_state),
            execution_mode=ExecutionMode(model.execution_mode.value),
            status=RunStatus(model.status.value),
            context=dict(model.context or {}),
            started_at=model.started_at,
            completed_at=model.completed_at,
        )

    def create(self, run: WorkflowRun) -> WorkflowRun:
        from forge_db.models.enums import ExecutionMode as DbExecutionMode
        from forge_db.models.enums import RunStatus as DbRunStatus
        from forge_db.models.enums import WorkflowState as DbWorkflowState
        from forge_db.models.runs import WorkflowRun as DbWorkflowRun

        model = DbWorkflowRun(
            workspace_id=self._workspace_id,
            task_id=run.task_id,
            workflow_name=run.workflow_name,
            current_state=DbWorkflowState(run.current_state),
            execution_mode=DbExecutionMode(run.execution_mode.value),
            status=DbRunStatus(run.status.value),
            context=dict(run.context),
            started_at=run.started_at,
            completed_at=run.completed_at,
        )
        if run.id is not None:
            model.id = run.id
        self._session.add(model)
        self._session.flush()
        return self._to_dto(model)

    def _load(self, run_id: uuid.UUID) -> DbWorkflowRun:
        from forge_db.models.runs import WorkflowRun as DbWorkflowRun

        model = self._session.get(DbWorkflowRun, run_id)
        if model is None:
            raise WorkflowRunNotFoundError(run_id)
        return model

    def get(self, run_id: uuid.UUID) -> WorkflowRun:
        return self._to_dto(self._load(run_id))

    def update(self, run: WorkflowRun) -> WorkflowRun:
        from forge_db.models.enums import ExecutionMode as DbExecutionMode
        from forge_db.models.enums import RunStatus as DbRunStatus
        from forge_db.models.enums import WorkflowState as DbWorkflowState

        if run.id is None:
            raise WorkflowRunNotFoundError(run.id)
        model = self._load(run.id)
        model.workflow_name = run.workflow_name
        model.current_state = DbWorkflowState(run.current_state)
        model.execution_mode = DbExecutionMode(run.execution_mode.value)
        model.status = DbRunStatus(run.status.value)
        model.context = dict(run.context)
        model.started_at = run.started_at
        model.completed_at = run.completed_at
        self._session.flush()
        return self._to_dto(model)


__all__ = [
    "InMemoryWorkflowStore",
    "SqlAlchemyWorkflowStore",
    "WorkflowStore",
]
