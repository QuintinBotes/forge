"""Persistence for workflow runs.

The engine reads and writes :class:`~scf_contracts.WorkflowRun` DTOs through a
:class:`WorkflowStore`. Two implementations ship:

* :class:`InMemoryWorkflowStore` — the unit-test / ephemeral backend; stores
  deep copies so a returned DTO can't mutate engine state by reference.
* :class:`SqlAlchemyWorkflowStore` — the Postgres-backed production store
  (spec: "Postgres FSM"), mapping the DTO onto the ``scf_db`` ORM row.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from scf_contracts import ExecutionMode, RunStatus, WorkflowRun
from scf_workflow.exceptions import DuplicateRunError, WorkflowRunNotFoundError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from scf_db.models.runs import WorkflowRun as DbWorkflowRun
    from scf_db.models.runs import WorkflowStationVisit
    from scf_workflow.temporal.payloads import StationVisitRecord


#: Terminal run statuses — used to decide whether a task already has a live run.
_TERMINAL_STATUSES = frozenset({RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED})

#: How far a station visit has got (spec K §4.3); 2 is finished. Status only moves forward.
STATION_STATUS_RANK = {
    "running": 0,
    "waiting": 1,
    "succeeded": 2,
    "failed": 2,
    "parked": 2,
    "cancelled": 2,
}
STATION_KINDS = frozenset({"agent", "service", "human"})


@runtime_checkable
class WorkflowStore(Protocol):
    """Durable store for workflow runs."""

    def create(self, run: WorkflowRun) -> WorkflowRun: ...

    def get(self, run_id: uuid.UUID) -> WorkflowRun: ...

    def update(self, run: WorkflowRun) -> WorkflowRun: ...

    def find_active_by_task(self, task_id: uuid.UUID) -> WorkflowRun | None:
        """Return a non-terminal run for ``task_id`` if one exists (F25)."""
        ...

    def list_by_task(self, task_id: uuid.UUID) -> list[WorkflowRun]:
        """Return all runs for ``task_id`` (F25; used by ``list_runs``)."""
        ...


@runtime_checkable
class StationVisitStore(Protocol):
    """A store that also records a line run's station visits (spec K §4.3)."""

    def save_station_visit(self, visit: StationVisitRecord) -> None:
        """Upsert ``visit`` by its id; a status that does not move forward changes nothing."""
        ...

    def list_station_visits(self, run_id: uuid.UUID) -> list[StationVisitRecord]:
        """The run's visits in the order they started."""
        ...


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

    def find_active_by_task(self, task_id: uuid.UUID) -> WorkflowRun | None:
        for run in self._runs.values():
            if run.task_id == task_id and run.status not in _TERMINAL_STATUSES:
                return run.model_copy(deep=True)
        return None

    def list_by_task(self, task_id: uuid.UUID) -> list[WorkflowRun]:
        return [run.model_copy(deep=True) for run in self._runs.values() if run.task_id == task_id]


class SqlAlchemyWorkflowStore:
    """Postgres-backed :class:`WorkflowStore` over the ``scf_db`` ORM."""

    def __init__(self, session: Session, *, workspace_id: uuid.UUID) -> None:
        self._session = session
        self._workspace_id = workspace_id

    @property
    def workspace_id(self) -> uuid.UUID:
        return self._workspace_id

    @staticmethod
    def _to_dto(model: DbWorkflowRun) -> WorkflowRun:
        # ``scf_db`` defines its own enum classes (same values as the contract
        # enums); convert by value so the DTO carries the contract types. The
        # ``WorkflowRun`` contract DTO is frozen (carries no engine columns), so
        # F25's engine attribution rides in ``context`` — but only for non-default
        # (temporal) runs, so a V1 FSM run's context stays byte-for-byte unchanged.
        context = dict(model.context or {})
        from scf_db.models.enums import EngineBackend as DbEngineBackend

        if model.engine_backend != DbEngineBackend.POSTGRES_FSM:
            context["engine_backend"] = model.engine_backend.value
        if model.temporal_workflow_id is not None:
            context["temporal_workflow_id"] = model.temporal_workflow_id
        if model.temporal_run_id is not None:
            context["temporal_run_id"] = model.temporal_run_id
        return WorkflowRun(
            id=model.id,
            task_id=model.task_id,
            workflow_name=model.workflow_name,
            current_state=str(model.current_state),
            execution_mode=ExecutionMode(model.execution_mode.value),
            status=RunStatus(model.status.value),
            context=context,
            started_at=model.started_at,
            completed_at=model.completed_at,
        )

    def create(self, run: WorkflowRun) -> WorkflowRun:
        from sqlalchemy.exc import IntegrityError

        from scf_db.models.enums import EngineBackend as DbEngineBackend
        from scf_db.models.enums import ExecutionMode as DbExecutionMode
        from scf_db.models.enums import RunStatus as DbRunStatus
        from scf_db.models.runs import WorkflowRun as DbWorkflowRun

        context, engine_backend, wf_id, run_id = self._split_engine_context(run.context)
        model = DbWorkflowRun(
            workspace_id=self._workspace_id,
            task_id=run.task_id,
            workflow_name=run.workflow_name,
            current_state=run.current_state,
            execution_mode=DbExecutionMode(run.execution_mode.value),
            status=DbRunStatus(run.status.value),
            context=context,
            engine_backend=DbEngineBackend(engine_backend),
            temporal_workflow_id=wf_id,
            temporal_run_id=run_id,
            started_at=run.started_at,
            completed_at=run.completed_at,
        )
        revision = context.get("definition_revision_id")
        if revision is not None:
            model.definition_revision_id = uuid.UUID(str(revision))
        if run.id is not None:
            model.id = run.id
        self._session.add(model)
        try:
            self._session.flush()
        except IntegrityError as exc:
            if engine_backend != "temporal" or run.task_id is None:
                raise
            self._session.rollback()
            # ``uq_workflow_run_active_task``: a concurrent start already holds the task.
            if self.find_active_by_task(run.task_id) is not None:
                raise DuplicateRunError(run.task_id) from exc
            raise
        return self._to_dto(model)

    @staticmethod
    def _split_engine_context(
        context: dict[str, object],
    ) -> tuple[dict[str, object], str, str | None, str | None]:
        """Pull engine-attribution keys out of ``context`` into column values."""
        ctx = {
            k: v
            for k, v in context.items()
            if k not in {"engine_backend", "temporal_workflow_id", "temporal_run_id"}
        }
        engine_backend = str(context.get("engine_backend", "postgres_fsm"))
        wf_id = context.get("temporal_workflow_id")
        run_id = context.get("temporal_run_id")
        return (
            ctx,
            engine_backend,
            str(wf_id) if wf_id is not None else None,
            str(run_id) if run_id is not None else None,
        )

    def _load(self, run_id: uuid.UUID) -> DbWorkflowRun:
        from scf_db.models.runs import WorkflowRun as DbWorkflowRun

        model = self._session.get(DbWorkflowRun, run_id)
        # Ownership is the row's workspace: a foreign run reads as missing.
        if model is None or model.workspace_id != self._workspace_id:
            raise WorkflowRunNotFoundError(run_id)
        return model

    def get(self, run_id: uuid.UUID) -> WorkflowRun:
        return self._to_dto(self._load(run_id))

    def update(self, run: WorkflowRun) -> WorkflowRun:
        from scf_db.models.enums import EngineBackend as DbEngineBackend
        from scf_db.models.enums import ExecutionMode as DbExecutionMode
        from scf_db.models.enums import RunStatus as DbRunStatus

        if run.id is None:
            raise WorkflowRunNotFoundError(run.id)
        model = self._load(run.id)
        context, engine_backend, wf_id, run_id = self._split_engine_context(run.context)
        model.workflow_name = run.workflow_name
        model.current_state = run.current_state
        model.execution_mode = DbExecutionMode(run.execution_mode.value)
        model.status = DbRunStatus(run.status.value)
        model.context = context
        model.engine_backend = DbEngineBackend(engine_backend)
        if wf_id is not None:
            model.temporal_workflow_id = wf_id
        if run_id is not None:
            model.temporal_run_id = run_id
        model.started_at = run.started_at
        model.completed_at = run.completed_at
        self._session.flush()
        return self._to_dto(model)

    def find_active_by_task(self, task_id: uuid.UUID) -> WorkflowRun | None:
        from sqlalchemy import select

        from scf_db.models.enums import RunStatus as DbRunStatus
        from scf_db.models.runs import WorkflowRun as DbWorkflowRun

        terminal = [DbRunStatus(s.value) for s in _TERMINAL_STATUSES]
        stmt = (
            select(DbWorkflowRun)
            .where(DbWorkflowRun.task_id == task_id)
            .where(DbWorkflowRun.workspace_id == self._workspace_id)
            .where(DbWorkflowRun.status.notin_(terminal))
        )
        model = self._session.execute(stmt).scalars().first()
        return self._to_dto(model) if model is not None else None

    def list_by_task(self, task_id: uuid.UUID) -> list[WorkflowRun]:
        from sqlalchemy import select

        from scf_db.models.runs import WorkflowRun as DbWorkflowRun

        stmt = (
            select(DbWorkflowRun)
            .where(DbWorkflowRun.task_id == task_id)
            .where(DbWorkflowRun.workspace_id == self._workspace_id)
            .order_by(DbWorkflowRun.started_at)
        )
        return [self._to_dto(m) for m in self._session.execute(stmt).scalars().all()]

    def save_station_visit(self, visit: StationVisitRecord) -> None:
        from datetime import UTC, datetime
        from decimal import Decimal

        from scf_db.models.runs import WorkflowStationVisit
        from scf_workflow.temporal.ids import station_visit_id

        self._load(visit.workflow_run_id)  # the run is this workspace's, or not found
        visit_id = station_visit_id(visit.workflow_run_id, visit.state, visit.visit)
        now = datetime.now(UTC)
        # Locked, so a late retry cannot interleave with a later report of the visit. Two
        # first reports racing to insert: one fails on the key, and its retry lands here.
        row = self._session.get(WorkflowStationVisit, visit_id, with_for_update=True)
        if row is None:
            row = WorkflowStationVisit(
                id=visit_id,
                workspace_id=self._workspace_id,
                workflow_run_id=visit.workflow_run_id,
                definition_revision_id=visit.definition_revision_id,
                state=visit.state,
                visit=visit.visit,
                kind=visit.kind,
                started_at=now,
            )
            self._session.add(row)
        elif STATION_STATUS_RANK[visit.status] <= STATION_STATUS_RANK[row.status]:
            if visit.status == "parked" and row.status in ("succeeded", "failed"):
                # Routing follows the done report (spec K §4.2): parking marks the finished
                # visit and may add a failure detail; its results and finish time stay.
                row.status = "parked"
                row.outcome = visit.outcome or row.outcome
                row.error_code = visit.error_code or row.error_code
                row.error_message = visit.error_message or row.error_message
                self._session.flush()
            return
        row.status = visit.status
        row.outcome = visit.outcome
        row.outputs = visit.outputs
        row.error_code = visit.error_code
        row.error_message = visit.error_message
        # A later report that leaves a link out keeps the one an earlier report made.
        row.service_name = visit.service_name or row.service_name
        row.agent_run_id = visit.agent_run_id or row.agent_run_id
        row.approval_request_id = visit.approval_request_id or row.approval_request_id
        row.tokens_used = visit.tokens_used
        row.cost_usd = None if visit.cost_usd is None else Decimal(str(visit.cost_usd))
        row.priced = visit.priced
        if STATION_STATUS_RANK[visit.status] == 2:
            row.finished_at = now
        self._session.flush()

    def list_station_visits(self, run_id: uuid.UUID) -> list[StationVisitRecord]:
        from sqlalchemy import select

        from scf_db.models.runs import WorkflowStationVisit as Visit

        self._load(run_id)
        stmt = (
            select(Visit)
            .where(Visit.workspace_id == self._workspace_id, Visit.workflow_run_id == run_id)
            .order_by(Visit.started_at, Visit.visit)
        )
        return [_visit_record(row) for row in self._session.scalars(stmt)]


def _visit_record(row: WorkflowStationVisit) -> StationVisitRecord:
    from dataclasses import fields

    from scf_workflow.temporal.payloads import StationVisitRecord

    values = {f.name: getattr(row, f.name) for f in fields(StationVisitRecord)}
    values["cost_usd"] = None if row.cost_usd is None else float(row.cost_usd)
    return StationVisitRecord(**values)


__all__ = [
    "STATION_KINDS",
    "STATION_STATUS_RANK",
    "InMemoryWorkflowStore",
    "SqlAlchemyWorkflowStore",
    "StationVisitStore",
    "WorkflowStore",
]
