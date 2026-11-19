"""``forge.create_approval`` (spec K P0, A1): the worker opens a feature run's
gate request through the approval service on the database repository."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker
from temporalio.exceptions import ApplicationError

import scf_worker.temporal_main as tm
from scf_api.services.approval_repository_db import SqlAlchemyApprovalRepository
from scf_approval.models import GateStatus, GateType
from scf_db.base import Base
from scf_db.models import Project, Task, WorkflowRun, Workspace
from scf_worker.approval_activity import make_create_approval_fn
from scf_workflow.temporal.activities import WorkflowActivities
from scf_workflow.temporal.payloads import ApprovalInput

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


async def test_the_spec_request_names_the_drafted_scenarios_by_id_and_title(
    factory: sessionmaker[Session],
) -> None:
    """#288 criterion 1: ids and titles only; a withdrawn one is not named, and only
    the spec gate names them."""
    from scf_api.schemas.held_out import HeldOutScenarioCreate
    from scf_api.services.held_out import store_agent_draft

    task_id, run_id = _seed_run(factory)
    body = HeldOutScenarioCreate.model_validate(
        {
            "title": "Refunds settle",
            "files": [{"path": "t.py", "content": "SECRET"}],
            "command": "x",
        }
    )
    with factory() as s:
        withdrawn, drafted = (
            store_agent_draft(
                s,
                workspace_id=WS,
                task_id=task_id,
                body=body,
                drafted_by="agent:verifier/gpt-6-astra",
                agent_run_id=uuid.uuid4(),
                workflow_run_id=run_id,
            )
            for _ in range(2)
        )
        withdrawn.state = "withdrawn"
        s.commit()
    create = make_create_approval_fn(factory)

    await create(_input(task_id, run_id))
    await create(_input(task_id, run_id, "plan"))

    requests = await SqlAlchemyApprovalRepository(factory).list(workspace_id=WS)
    by_gate = {r.gate_type: r.gate_payload for r in requests}
    assert by_gate[GateType.SPEC]["held_out_scenarios"] == [
        {"id": str(drafted.id), "title": "Refunds settle"}
    ]
    assert "SECRET" not in str(by_gate[GateType.SPEC])
    assert "held_out_scenarios" not in by_gate[GateType.PLAN]


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
