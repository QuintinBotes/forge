"""``forge.create_approval`` (spec K P0, A1): the worker opens a feature run's
gate request through the approval service on the database repository."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker
from temporalio.exceptions import ApplicationError

import forge_worker.temporal_main as tm
from forge_api.services.approval_repository_db import SqlAlchemyApprovalRepository
from forge_approval.models import GateStatus, GateType
from forge_db.base import Base
from forge_db.models import Project, Task, WorkflowRun, Workspace
from forge_worker.approval_activity import make_create_approval_fn
from forge_workflow.temporal.activities import WorkflowActivities
from forge_workflow.temporal.payloads import ApprovalInput

WS = uuid.UUID("00000000-0000-0000-0000-0000000000d1")


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    engine.dispose()


def _seed_run(factory: sessionmaker[Session]) -> tuple[uuid.UUID, uuid.UUID]:
    """A workspace task with a workflow run; returns ``(task_id, run_id)``."""
    with factory() as s:
        s.add(Workspace(id=WS, name="Acme", slug=f"acme-{uuid.uuid4().hex[:6]}"))
        s.flush()
        project = Project(workspace_id=WS, name="Core", key=f"C{uuid.uuid4().hex[:4]}")
        s.add(project)
        s.flush()
        task = Task(workspace_id=WS, project_id=project.id, key="TASK-1", title="ship the gate")
        s.add(task)
        s.flush()
        run = WorkflowRun(workspace_id=WS, task_id=task.id)
        s.add(run)
        s.commit()
        return task.id, run.id


def _input(task_id: uuid.UUID, run_id: uuid.UUID, gate: str = "spec") -> ApprovalInput:
    """What ``FeatureWorkflow._create_approval`` sends."""
    return ApprovalInput(
        workflow_run_id=run_id,
        workspace_id=WS,
        task_id=task_id,
        gate=gate,
        summary=f"{gate} approval for run {run_id}",
        idempotency_key=f"{run_id}:approval:{gate}:3",
    )


async def test_create_approval_opens_one_workflow_run_request_while_pending(
    factory: sessionmaker[Session],
) -> None:
    task_id, run_id = _seed_run(factory)
    create = make_create_approval_fn(factory)

    first = await create(_input(task_id, run_id))
    second = await create(_input(task_id, run_id))  # an activity retry

    assert second == first
    [request] = await SqlAlchemyApprovalRepository(factory).list(workspace_id=WS)
    assert request.id == first
    assert request.gate_type is GateType.SPEC
    assert request.status is GateStatus.PENDING
    assert request.subject_type == "workflow_run"
    assert request.subject_id == run_id
    assert request.workflow_run_id == run_id
    assert request.task_id == task_id
    assert request.requested_actor == f"agent:{run_id}"
    assert request.title == "ship the gate"

    # Idempotency is per gate: the plan gate is its own request.
    assert await create(_input(task_id, run_id, "plan")) != first


async def test_the_worker_wires_create_approval_without_fakes(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FORGE_ALLOW_FAKE_ACTIVITIES", raising=False)
    task_id, run_id = _seed_run(factory)

    request_id = await tm.build_activities(factory).create_approval(_input(task_id, run_id))

    [request] = await SqlAlchemyApprovalRepository(factory).list(workspace_id=WS)
    assert str(request.id) == request_id


async def test_an_unwired_create_approval_refuses() -> None:
    """What the worker did before spec K P0 (the contrast to the test above)."""
    with pytest.raises(ApplicationError) as err:
        await WorkflowActivities().create_approval(_input(uuid.uuid4(), uuid.uuid4()))
    assert err.value.type == "ActivityNotConfigured"
