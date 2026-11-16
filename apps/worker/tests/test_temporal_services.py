"""Services bound to the Temporal activities (issue #170, first slice).

Every test goes through ``build_activities`` with ``FORGE_ALLOW_FAKE_ACTIVITIES``
unset, so a passing assertion is about the worker's real wiring.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from scf_db.base import Base
from scf_db.models import (
    ApprovalRequest,
    ApprovalStatus,
    Project,
    RedTeamRecord,
    Task,
    WorkflowRun,
    Workspace,
)
from scf_db.models.observability_audit import ObservabilityAuditEntry
from scf_worker.temporal_main import build_activities
from scf_workflow.temporal.activities import WorkflowActivities
from scf_workflow.temporal.payloads import (
    ApprovalInput,
    OpenPrInput,
    RedTeamInput,
    ResumeAgentInput,
    RunChecksInput,
)


@dataclass(frozen=True)
class Seeded:
    workspace_id: uuid.UUID
    task_id: uuid.UUID
    run_id: uuid.UUID


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    # One shared connection: the approval service runs on a helper thread.
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


def _seed(factory: sessionmaker[Session]) -> Seeded:
    with factory() as session:
        ws = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
        session.add(ws)
        session.flush()
        project = Project(workspace_id=ws.id, name="Core", key=f"C{uuid.uuid4().hex[:4]}")
        session.add(project)
        session.flush()
        task = Task(workspace_id=ws.id, project_id=project.id, key="TASK-1", title="a task")
        session.add(task)
        session.flush()
        run = WorkflowRun(workspace_id=ws.id, task_id=task.id)
        session.add(run)
        session.commit()
        return Seeded(workspace_id=ws.id, task_id=task.id, run_id=run.id)


@pytest.fixture
def activities(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> WorkflowActivities:
    monkeypatch.delenv("FORGE_ALLOW_FAKE_ACTIVITIES", raising=False)
    return build_activities(factory)


def _red_team_input(seeded: Seeded) -> RedTeamInput:
    return RedTeamInput(
        workflow_run_id=seeded.run_id,
        workspace_id=seeded.workspace_id,
        task_id=seeded.task_id,
        idempotency_key=f"{seeded.run_id}:red_team:spec",
    )


def _approval_input(seeded: Seeded, gate: str = "pr", seq: int = 9) -> ApprovalInput:
    return ApprovalInput(
        workflow_run_id=seeded.run_id,
        workspace_id=seeded.workspace_id,
        task_id=seeded.task_id,
        gate=gate,
        summary=f"{gate} approval for run {seeded.run_id}",
        idempotency_key=f"{seeded.run_id}:approval:{gate}:{seq}",
    )


async def test_red_team_scan_without_an_adversary_records_one_parked_row(
    activities: WorkflowActivities, factory: sessionmaker[Session]
) -> None:
    seeded = _seed(factory)
    first = await ActivityEnvironment().run(activities.run_red_team_scan, _red_team_input(seeded))
    again = await ActivityEnvironment().run(activities.run_red_team_scan, _red_team_input(seeded))

    assert (first.verdict, first.kind) == ("survived", "parked")
    assert first.evidence == {"parked": True, "reason": "no adversary model/sandbox wired"}
    assert again == first
    with factory() as session:
        rows = session.scalars(select(RedTeamRecord)).all()
    assert [(r.workflow_run_id, r.workspace_id, r.kind) for r in rows] == [
        (seeded.run_id, seeded.workspace_id, "parked")
    ]


async def test_create_approval_opens_one_gate_tied_to_the_workflow_run(
    activities: WorkflowActivities, factory: sessionmaker[Session]
) -> None:
    seeded = _seed(factory)
    first = await ActivityEnvironment().run(activities.create_approval, _approval_input(seeded))
    retry = await ActivityEnvironment().run(activities.create_approval, _approval_input(seeded))

    assert retry == first
    with factory() as session:
        (row,) = session.scalars(select(ApprovalRequest)).all()
    assert str(row.id) == first
    assert row.workflow_run_id == seeded.run_id
    assert row.gate.value == "pr"
    assert (row.subject_type, row.subject_id) == ("workflow_run", seeded.run_id)
    assert row.task_id == seeded.task_id
    assert row.status is ApprovalStatus.PENDING


async def test_create_approval_writes_one_requested_entry_to_the_audit_chain(
    activities: WorkflowActivities, factory: sessionmaker[Session]
) -> None:
    seeded = _seed(factory)
    await ActivityEnvironment().run(activities.create_approval, _approval_input(seeded))
    await ActivityEnvironment().run(activities.create_approval, _approval_input(seeded))

    with factory() as session:
        (entry,) = session.scalars(select(ObservabilityAuditEntry)).all()
    assert (entry.category, entry.action) == ("approval", "pr")
    assert entry.run_id == seeded.run_id


async def test_create_approval_retry_after_the_gate_was_decided_opens_no_second_gate(
    activities: WorkflowActivities, factory: sessionmaker[Session]
) -> None:
    seeded = _seed(factory)
    first = await ActivityEnvironment().run(activities.create_approval, _approval_input(seeded))
    with factory() as session:
        session.get_one(ApprovalRequest, uuid.UUID(first)).status = ApprovalStatus.APPROVED
        session.commit()

    retry = await ActivityEnvironment().run(activities.create_approval, _approval_input(seeded))

    assert retry == first
    with factory() as session:
        assert len(session.scalars(select(ApprovalRequest)).all()) == 1


async def test_a_new_gate_entry_after_a_decision_opens_a_new_request(
    activities: WorkflowActivities, factory: sessionmaker[Session]
) -> None:
    seeded = _seed(factory)
    first = await ActivityEnvironment().run(
        activities.create_approval, _approval_input(seeded, "spec", seq=3)
    )
    with factory() as session:
        session.get_one(ApprovalRequest, uuid.UUID(first)).status = ApprovalStatus.REJECTED
        session.commit()

    second = await ActivityEnvironment().run(
        activities.create_approval, _approval_input(seeded, "spec", seq=5)
    )

    assert second != first


@pytest.mark.parametrize("method", ["run_red_team_scan", "create_approval"])
async def test_a_run_outside_the_callers_workspace_is_refused(
    activities: WorkflowActivities, factory: sessionmaker[Session], method: str
) -> None:
    seeded = _seed(factory)
    foreign = Seeded(workspace_id=uuid.uuid4(), task_id=seeded.task_id, run_id=seeded.run_id)
    make = _red_team_input if method == "run_red_team_scan" else _approval_input

    with pytest.raises(ApplicationError) as caught:
        await ActivityEnvironment().run(getattr(activities, method), make(foreign))

    assert caught.value.type == "WorkflowRunNotFound"
    assert caught.value.non_retryable is True
    with factory() as session:
        assert session.scalars(select(RedTeamRecord)).all() == []
        assert session.scalars(select(ApprovalRequest)).all() == []


@pytest.mark.parametrize("method", ["run_red_team_scan", "create_approval"])
async def test_a_run_that_does_not_exist_is_refused(
    activities: WorkflowActivities, factory: sessionmaker[Session], method: str
) -> None:
    seeded = _seed(factory)
    missing = Seeded(workspace_id=seeded.workspace_id, task_id=seeded.task_id, run_id=uuid.uuid4())
    make = _red_team_input if method == "run_red_team_scan" else _approval_input

    with pytest.raises(ApplicationError) as caught:
        await ActivityEnvironment().run(getattr(activities, method), make(missing))

    assert caught.value.type == "WorkflowRunNotFound"
    assert caught.value.non_retryable is True
    with factory() as session:
        assert session.scalars(select(RedTeamRecord)).all() == []
        assert session.scalars(select(ApprovalRequest)).all() == []


RUN, WS, TASK = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


@pytest.mark.parametrize(
    ("method", "payload"),
    [
        # load_guard_inputs, run_agent and pause_and_notify are bound by #250 and
        # tested in test_temporal_guard_inputs.py and test_temporal_run_agent.py.
        ("resume_agent", ResumeAgentInput(RUN, TASK, WS, None, attempt=0, idempotency_key="k")),
        ("run_checks", RunChecksInput(RUN, WS, attempt=0, idempotency_key="k")),
        ("open_pr_with_spec_traceability", OpenPrInput(RUN, WS, "b", idempotency_key="k")),
    ],
)
async def test_activities_this_slice_does_not_bind_still_refuse(
    activities: WorkflowActivities, method: str, payload: object
) -> None:
    with pytest.raises(ApplicationError) as caught:
        await ActivityEnvironment().run(getattr(activities, method), payload)
    assert caught.value.type == "ActivityNotConfigured"
    assert caught.value.non_retryable is True


async def test_feature_workflow_reaches_its_first_human_gate_with_real_records(
    activities: WorkflowActivities, factory: sessionmaker[Session]
) -> None:
    from temporalio.contrib.pydantic import pydantic_data_converter
    from temporalio.testing import WorkflowEnvironment

    from scf_workflow.temporal.payloads import WorkflowParams
    from scf_workflow.temporal.worker import build_temporal_worker
    from scf_workflow.temporal.workflows import FeatureWorkflow

    try:
        env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    except Exception as exc:  # pragma: no cover - offline sandbox
        pytest.skip(f"Temporal test server unavailable: {exc}")
    seeded = _seed(factory)
    try:
        async with build_temporal_worker(env.client, activities, task_queue="forge-170"):
            handle = await env.client.start_workflow(
                FeatureWorkflow.run,
                WorkflowParams(
                    workflow_run_id=seeded.run_id,
                    task_id=seeded.task_id,
                    workspace_id=seeded.workspace_id,
                ),
                id=f"forge-170-{seeded.run_id}",
                task_queue="forge-170",
            )
            awaiting: list[str] = []
            for _ in range(300):
                awaiting = await handle.query(FeatureWorkflow.awaiting)
                if awaiting:
                    break
                await asyncio.sleep(0.02)
            await handle.terminate()
    finally:
        await env.shutdown()

    assert set(awaiting) == {"spec_approved_by_human", "spec_changes_requested"}
    with factory() as session:
        (record,) = session.scalars(select(RedTeamRecord)).all()
        (gate,) = session.scalars(select(ApprovalRequest)).all()
        run = session.get_one(WorkflowRun, seeded.run_id)
    assert (record.workflow_run_id, record.kind) == (seeded.run_id, "parked")
    assert (gate.workflow_run_id, gate.gate.value, gate.status) == (
        seeded.run_id,
        "spec",
        ApprovalStatus.PENDING,
    )
    assert run.current_state == "spec_review"
