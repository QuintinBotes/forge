"""Spec K P2 activity seams: station run status, heartbeating agents, cancel attribution."""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from datetime import timedelta
from typing import Any

import pytest
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.testing import ActivityEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

import forge_workflow.temporal.activities as activities_module
from forge_contracts import RunStatus, WorkflowRun
from forge_workflow.store import InMemoryWorkflowStore
from forge_workflow.temporal.activities import WorkflowActivities
from forge_workflow.temporal.payloads import (
    AgentRunResultDTO,
    ResumeAgentInput,
    RunAgentInput,
    TransitionRecord,
)

pytestmark = pytest.mark.asyncio

WS = uuid.uuid4()


def _store_with_run(state: str = "implement") -> tuple[InMemoryWorkflowStore, uuid.UUID]:
    store = InMemoryWorkflowStore()
    run = store.create(
        WorkflowRun(task_id=uuid.uuid4(), current_state=state, status=RunStatus.RUNNING)
    )
    assert run.id is not None
    return store, run.id


def _rec(run_id: uuid.UUID, to_state: str, **extra: object) -> TransitionRecord:
    return TransitionRecord(
        workflow_run_id=run_id,
        workspace_id=WS,
        from_state="implement",
        to_state=to_state,
        event=str(extra.pop("event", "succeeded")),
        idempotency_key=f"{run_id}:transition:{to_state}",
        **extra,  # type: ignore[arg-type]
    )


# -- decision 3: persist_transition honours run_status --------------------- #


async def test_run_status_sets_the_status_a_station_terminal_names() -> None:
    store, run_id = _store_with_run()
    acts = WorkflowActivities(store=store)

    await acts.persist_transition(_rec(run_id, "shipped", run_status="succeeded"))

    run = store.get(run_id)
    assert (run.current_state, run.status) == ("shipped", RunStatus.SUCCEEDED)
    assert run.completed_at is not None


async def test_run_status_escalated_parks_without_completing() -> None:
    store, run_id = _store_with_run()

    await WorkflowActivities(store=store).persist_transition(
        _rec(run_id, "implement", run_status="escalated")
    )

    run = store.get(run_id)
    assert run.status == RunStatus.ESCALATED
    assert run.completed_at is None


async def test_without_run_status_the_status_is_derived_as_before() -> None:
    store, run_id = _store_with_run()
    acts = WorkflowActivities(store=store)

    await acts.persist_transition(_rec(run_id, "shipped"))  # not a feature state
    assert store.get(run_id).status == RunStatus.RUNNING

    await acts.persist_transition(_rec(run_id, "cancelled"))
    run = store.get(run_id)
    assert run.status == RunStatus.CANCELLED
    assert run.completed_at is not None


# -- the engine's cancel carries its actor on the Signal -------------------- #


async def test_an_engine_cancel_records_its_actor(harness: Any) -> None:
    h = harness()
    async with h.worker():
        run = await h.start()
        await h.wait_awaiting(run, {"spec_approved_by_human"})
        await h.engine.atransition(
            run.id, "cancel", actor="user:decider", reason="gate_plan_rejected"
        )
        await h.handle(run).result()

    last = h.transitions(run)[-1]
    assert (last["event"], last["actor"]) == ("cancel", "user:decider")
    assert last["payload"] == {"reason": "gate_plan_rejected"}
    assert "cancel_actor" not in h.engine.get_run(run.id).context  # no run-context side channel


# -- a row whose start failed is never moved (spec K §4.11) ----------------- #


async def test_a_start_failed_row_is_not_revived_by_an_orphaned_workflow() -> None:
    store, run_id = _store_with_run("created")
    run = store.get(run_id)
    run.status = RunStatus.FAILED
    run.context["start_error"] = "TimeoutError: no StartWorkflowExecution response"
    store.update(run)

    recorded = await WorkflowActivities(store=store).persist_transition(_rec(run_id, "drafting"))

    assert recorded == 0

    after = store.get(run_id)
    assert (after.current_state, after.status) == ("created", RunStatus.FAILED)
    assert "transitions" not in after.context


async def test_a_failed_run_without_a_start_error_still_records() -> None:
    store, run_id = _store_with_run()
    run = store.get(run_id)
    run.status = RunStatus.FAILED
    store.update(run)

    assert await WorkflowActivities(store=store).persist_transition(_rec(run_id, "failed")) == 1


# -- decision 4: agent activities heartbeat and can be cancelled ------------ #


def _agent_input() -> RunAgentInput:
    return RunAgentInput(
        workflow_run_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        workspace_id=WS,
        attempt=0,
        idempotency_key="k",
    )


def _result() -> AgentRunResultDTO:
    return AgentRunResultDTO(agent_run_id=uuid.uuid4(), status="succeeded")


@pytest.fixture
def fast_heartbeat(monkeypatch: pytest.MonkeyPatch) -> float:
    interval = 0.02
    monkeypatch.setattr(activities_module, "AGENT_HEARTBEAT_EVERY", interval)
    return interval


async def test_a_long_agent_body_heartbeats_until_it_completes(fast_heartbeat: float) -> None:
    expected = _result()

    def slow_agent(_: RunAgentInput) -> AgentRunResultDTO:
        time.sleep(fast_heartbeat * 15)  # many heartbeat intervals
        return expected

    env = ActivityEnvironment()
    beats: list[object] = []
    env.on_heartbeat = lambda *details: beats.append(details)

    result = await env.run(WorkflowActivities(run_agent_fn=slow_agent).run_agent, _agent_input())

    assert result == expected
    assert len(beats) >= 5  # not just the one beat before the body


async def test_resume_agent_heartbeats_too(fast_heartbeat: float) -> None:
    def slow_resume(_: ResumeAgentInput) -> AgentRunResultDTO:
        time.sleep(fast_heartbeat * 10)
        return _result()

    env = ActivityEnvironment()
    beats: list[object] = []
    env.on_heartbeat = lambda *details: beats.append(details)
    inp = ResumeAgentInput(
        workflow_run_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        workspace_id=WS,
        agent_run_id=None,
        attempt=0,
        idempotency_key="k",
        input="use the staging bucket",
    )

    await env.run(WorkflowActivities(resume_agent_fn=slow_resume).resume_agent, inp)

    assert len(beats) >= 3


async def test_cancelling_a_running_agent_cancels_the_activity(fast_heartbeat: float) -> None:
    started, release = threading.Event(), threading.Event()

    def blocking_agent(_: RunAgentInput) -> AgentRunResultDTO:
        started.set()
        release.wait(5)
        return _result()

    env = ActivityEnvironment()
    task = asyncio.ensure_future(
        env.run(WorkflowActivities(run_agent_fn=blocking_agent).run_agent, _agent_input())
    )
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 2), 3)
        env.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    finally:
        release.set()  # the thread itself runs to completion (documented shortcut)


@workflow.defn(name="forge.test.AgentHeartbeatProbe")
class _AgentProbe:
    """Calls ``forge.run_agent`` with a heartbeat timeout shorter than the body."""

    @workflow.run
    async def run(self, inp: RunAgentInput) -> AgentRunResultDTO:
        return await workflow.execute_activity(
            "forge.run_agent",
            arg=inp,
            start_to_close_timeout=timedelta(seconds=60),
            heartbeat_timeout=timedelta(seconds=1),
            retry_policy=RetryPolicy(maximum_attempts=1),
            result_type=AgentRunResultDTO,
        )


async def test_an_agent_longer_than_the_heartbeat_timeout_completes(
    time_skip_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(activities_module, "AGENT_HEARTBEAT_EVERY", 0.1)
    expected = _result()

    def slow_agent(_: RunAgentInput) -> AgentRunResultDTO:
        time.sleep(1.6)  # past the 1 s heartbeat timeout
        return expected

    acts = WorkflowActivities(run_agent_fn=slow_agent)
    async with Worker(
        time_skip_env.client,
        task_queue="agent-heartbeat-probe",
        workflows=[_AgentProbe],
        activities=[acts.run_agent],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        result = await time_skip_env.client.execute_workflow(
            _AgentProbe.run,
            _agent_input(),
            id=f"probe-{uuid.uuid4()}",
            task_queue="agent-heartbeat-probe",
        )

    assert result == expected
