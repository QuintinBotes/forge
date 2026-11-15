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
from forge_contracts import RunStatus, WorkflowRun, WorkflowState
from forge_db.base import Base
from forge_db.models import Project, Task, Workspace
from forge_worker.approval_activity import make_create_approval_fn
from forge_workflow.store import InMemoryWorkflowStore, SqlAlchemyWorkflowStore, WorkflowStore
from forge_workflow.temporal.activities import WorkflowActivities
from forge_workflow.temporal.engine import TemporalWorkflowEngine
from forge_workflow.temporal.payloads import GuardInputs, GuardInputsRequest
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


def _plan_required(_: GuardInputsRequest) -> GuardInputs:
    """The permissive stand-in guard inputs, but with the plan gate on."""
    ready = ("repo_target_set", "policy_loaded", "skill_profile_set", "knowledge_synced")
    return GuardInputs(
        plan_required=True,
        preconditions=dict.fromkeys(ready, True),
        ci_status_green=True,
        spec_validated=True,
    )


async def _pending(factory: sessionmaker[Session], gate: GateType) -> Any:
    rows = await SqlAlchemyApprovalRepository(factory).list(workspace_id=WS)
    [request] = [r for r in rows if r.gate_type is gate and r.status is GateStatus.PENDING]
    return request


@pytest.mark.parametrize(
    ("action", "status"),
    [
        (ApprovalAction.REJECT, GateStatus.REJECTED),
        (ApprovalAction.REQUEST_CHANGES, GateStatus.CHANGES_REQUESTED),
    ],
)
@pytest.mark.parametrize(
    ("gate", "awaiting"),
    [(GateType.PLAN, "plan_approved_by_human"), (GateType.PR, "review_approved_by_human")],
)
async def test_rejecting_a_plan_or_pr_gate_cancels_the_durable_run(
    time_skip_env: Any,
    factory: sessionmaker[Session],
    gate: GateType,
    awaiting: str,
    action: ApprovalAction,
    status: GateStatus,
) -> None:
    """Spec K §4.11 on the Temporal engine: the hook cancels the run, the
    cancel transition carries the decider and the reason."""
    task_id = _seed_task(factory)
    store = InMemoryWorkflowStore()
    engine = _engine(time_skip_env, store)
    activities = WorkflowActivities(
        store=store,
        approval_fn=make_create_approval_fn(factory),
        guard_inputs_fn=_plan_required,
        allow_fakes=True,
    )

    @contextmanager
    def engine_for(workspace_id: uuid.UUID) -> Iterator[TemporalWorkflowEngine]:
        yield engine

    approvals = ApprovalService(
        SqlAlchemyApprovalRepository(factory),
        build_gate_registry(InMemoryGrantStore(), engine_factory=engine_for),
        ApprovalAuthorizer(),
    )
    approver = Principal(kind="user", id=uuid.uuid4(), role=Role.MEMBER, workspace_id=WS)
    decider = Principal(kind="user", id=uuid.uuid4(), role=Role.MEMBER, workspace_id=WS)
    approve = ApprovalDecisionRequest(decision=ApprovalAction.APPROVE)

    async with build_temporal_worker(time_skip_env.client, activities, task_queue=TASK_QUEUE):
        run = await engine.astart(task_id)
        assert run.id is not None
        await _wait_awaiting(engine, run, "spec_approved_by_human")
        spec = await _pending(factory, GateType.SPEC)
        await approvals.resolve(spec.id, approve, approver, workspace_id=WS)
        if gate is GateType.PR:
            await _wait_awaiting(engine, run, "plan_approved_by_human")
            plan = await _pending(factory, GateType.PLAN)
            await approvals.resolve(plan.id, approve, approver, workspace_id=WS)
        await _wait_awaiting(engine, run, awaiting)
        request = await _pending(factory, gate)

        resolution = await approvals.resolve(
            request.id, ApprovalDecisionRequest(decision=action), decider, workspace_id=WS
        )
        handle: Any = engine.workflow_handle(run.id)
        result = await handle.result()

    reason = f"gate_{gate.value}_{status.value}"
    assert resolution.status is status
    assert result.final_state is WorkflowState.CANCELLED
    projected = engine.get_run(run.id)
    assert (projected.current_state, projected.status) == ("cancelled", RunStatus.CANCELLED)
    last = engine.history(run.id)[-1]
    assert (last["event"], last["to_state"]) == ("cancel", "cancelled")
    assert last["actor"] == f"user:{decider.id}"
    assert last["payload"] == {"reason": reason}
    stored = await approvals.get(request.id, workspace_id=WS)
    assert stored.gate_payload["workflow_event"]["event"] == "cancel"
    assert stored.gate_payload["workflow_event"]["reason"] == reason


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
