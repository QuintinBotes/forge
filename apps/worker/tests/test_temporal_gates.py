"""Spec K P0 on the durable engine: a ``FeatureWorkflow`` run reaching its spec
gate opens the request through ``forge.create_approval`` (the worker's
DB-backed body), and resolving it runs the gate hook, whose
``submit_gate_event`` moves the run on through
``TemporalWorkflowEngine.atransition`` with the decider as actor.

Runs on the in-memory time-skipping Temporal test server and skips (never
fakes) when that server cannot start.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

import forge_worker.temporal_main as tm
from forge_api.services.approval_repository_db import SqlAlchemyApprovalRepository
from forge_api.services.approval_service import build_gate_registry
from forge_approval import ApprovalAuthorizer, ApprovalService
from forge_approval.models import (
    ApprovalAction,
    ApprovalDecisionRequest,
    GateStatus,
    GateType,
    Principal,
    Role,
)
from forge_approval.providers import InMemoryGrantStore
from forge_contracts import RunStatus, WorkflowRun
from forge_db.base import Base
from forge_db.models import Project, Task, Workspace
from forge_worker.approval_activity import make_create_approval_fn
from forge_workflow.store import InMemoryWorkflowStore, SqlAlchemyWorkflowStore, WorkflowStore
from forge_workflow.temporal.activities import WorkflowActivities
from forge_workflow.temporal.engine import TemporalWorkflowEngine
from forge_workflow.temporal.worker import build_temporal_worker
from forge_workflow.temporal.workflows import FeatureWorkflow

WS = uuid.UUID("00000000-0000-0000-0000-0000000000d2")
TASK_QUEUE = "forge-k-p0-gates"


@pytest.fixture
async def time_skip_env() -> AsyncIterator[Any]:
    """As ``packages/workflow-engine/tests/temporal/conftest.py``."""
    from temporalio.contrib.pydantic import pydantic_data_converter
    from temporalio.testing import WorkflowEnvironment

    try:
        env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    except Exception as exc:  # pragma: no cover - offline sandbox
        pytest.skip(f"PARKED: Temporal test server unavailable: {exc}")
    try:
        yield env
    finally:
        await env.shutdown()


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    engine.dispose()


def _seed_task(factory: sessionmaker[Session]) -> uuid.UUID:
    with factory() as s:
        s.add(Workspace(id=WS, name="Acme", slug="acme"))
        s.flush()
        project = Project(workspace_id=WS, name="Core", key="CORE")
        s.add(project)
        s.flush()
        task = Task(workspace_id=WS, project_id=project.id, key="TASK-1", title="durable gate")
        s.add(task)
        s.commit()
        return task.id


def _engine(env: Any, store: WorkflowStore) -> TemporalWorkflowEngine:
    return TemporalWorkflowEngine(
        workspace_id=WS, store=store, client=env.client, task_queue=TASK_QUEUE
    )


async def _wait_awaiting(engine: TemporalWorkflowEngine, run: WorkflowRun, event: str) -> None:
    assert run.id is not None
    handle: Any = engine.workflow_handle(run.id)
    last: list[str] = []
    for _ in range(300):
        last = await handle.query(FeatureWorkflow.awaiting)
        if event in last:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"workflow never awaited {event!r}; last={last}")


async def test_spec_gate_opens_a_request_and_its_approval_moves_the_run(
    time_skip_env: Any, factory: sessionmaker[Session]
) -> None:
    task_id = _seed_task(factory)
    store = InMemoryWorkflowStore()
    engine = _engine(time_skip_env, store)
    activities = WorkflowActivities(
        store=store, approval_fn=make_create_approval_fn(factory), allow_fakes=True
    )

    @contextmanager
    def engine_for(workspace_id: uuid.UUID) -> Iterator[TemporalWorkflowEngine]:
        assert workspace_id == WS
        yield engine

    approvals = ApprovalService(
        SqlAlchemyApprovalRepository(factory),
        build_gate_registry(InMemoryGrantStore(), engine_factory=engine_for),
        ApprovalAuthorizer(),
    )
    decider = Principal(kind="user", id=uuid.uuid4(), role=Role.MEMBER, workspace_id=WS)

    async with build_temporal_worker(time_skip_env.client, activities, task_queue=TASK_QUEUE):
        run = await engine.astart(task_id)
        await _wait_awaiting(engine, run, "spec_approved_by_human")

        [request] = await SqlAlchemyApprovalRepository(factory).list(workspace_id=WS)
        assert request.gate_type is GateType.SPEC
        assert request.subject_type == "workflow_run"
        assert request.workflow_run_id == run.id
        assert request.requested_actor == f"agent:{run.id}"
        assert request.title == "durable gate"

        resolution = await approvals.resolve(
            request.id,
            ApprovalDecisionRequest(decision=ApprovalAction.APPROVE),
            decider,
            workspace_id=WS,
        )

        assert resolution.status is GateStatus.APPROVED
        stored = await approvals.get(request.id, workspace_id=WS)
        assert stored.gate_payload["workflow_event"] == {
            "status": "submitted",
            "event": "spec_approved_by_human",
            "actor": f"user:{decider.id}",
            "to_state": "spec_approved",
        }
        assert run.id is not None
        [approved] = [t for t in engine.history(run.id) if t["event"] == "spec_approved_by_human"]
        assert (approved["from_state"], approved["to_state"]) == ("spec_review", "spec_approved")
        assert approved["actor"] == f"user:{decider.id}"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "production bug: the worker's SessionPerCallStore reads runs as the nil "
        "workspace, and SqlAlchemyWorkflowStore treats a run in any other "
        "workspace as missing (8010d86), so forge.persist_transition never finds "
        "a run the API created and retries forever before the first gate"
    ),
)
async def test_a_run_on_the_workers_activities_reaches_its_spec_gate(
    time_skip_env: Any, factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production wiring end to end: ``build_activities`` over the
    database (fakes only for the agent/red-team stand-ins), the run created
    in the API's workspace-scoped store."""
    monkeypatch.setenv("FORGE_ALLOW_FAKE_ACTIVITIES", "1")
    task_id = _seed_task(factory)
    activities = tm.build_activities(factory)

    with factory() as session:
        engine = _engine(time_skip_env, SqlAlchemyWorkflowStore(session, workspace_id=WS))
        async with build_temporal_worker(time_skip_env.client, activities, task_queue=TASK_QUEUE):
            run = await engine.astart(task_id)
            session.commit()  # as the API does after start
            await _wait_awaiting(engine, run, "spec_approved_by_human")

    [request] = await SqlAlchemyApprovalRepository(factory).list(workspace_id=WS)
    assert request.workflow_run_id == run.id


@pytest.mark.xfail(strict=True, reason="production bug: see the test above")
def test_the_workers_store_reads_a_run_the_api_created(factory: sessionmaker[Session]) -> None:
    task_id = _seed_task(factory)
    with factory() as s:
        run = SqlAlchemyWorkflowStore(s, workspace_id=WS).create(
            WorkflowRun(
                task_id=task_id,
                workflow_name="default_feature",
                current_state="created",
                status=RunStatus.RUNNING,
            )
        )
        s.commit()
    assert run.id is not None

    assert tm.SessionPerCallStore(factory).get(run.id).id == run.id
