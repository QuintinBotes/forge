"""Fixtures for the Temporal integration tier (F25 §7).

``time_skip_env`` yields an in-memory **time-skipping** ``WorkflowEnvironment`` so
durable-timer assertions (30/60/120s backoff) are instant. ``harness`` builds a
worker (``FeatureWorkflow`` + real ``WorkflowActivities`` over an in-memory
projection store) plus a ``TemporalWorkflowEngine`` sharing that store, with
scriptable activity callables for driving specific paths.

These tests need the Temporal test-server binary (downloaded once by the SDK). If
it cannot start (offline sandbox), the whole tier is skipped — never faked.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import pytest

from forge_contracts import WorkflowRun
from forge_workflow.store import InMemoryWorkflowStore
from forge_workflow.temporal.activities import WorkflowActivities
from forge_workflow.temporal.engine import TemporalWorkflowEngine
from forge_workflow.temporal.worker import build_temporal_worker
from forge_workflow.temporal.workflows import FeatureWorkflow

TASK_QUEUE = "forge-feature-test"


@pytest.fixture
async def time_skip_env() -> Any:
    from temporalio.contrib.pydantic import pydantic_data_converter
    from temporalio.testing import WorkflowEnvironment

    try:
        env = await WorkflowEnvironment.start_time_skipping(
            data_converter=pydantic_data_converter
        )
    except Exception as exc:  # pragma: no cover - offline sandbox
        pytest.skip(f"PARKED: Temporal test server unavailable: {exc}")
    try:
        yield env
    finally:
        await env.shutdown()


@dataclass
class Harness:
    env: Any
    store: InMemoryWorkflowStore
    activities: WorkflowActivities
    engine: TemporalWorkflowEngine
    workspace_id: uuid.UUID
    _runs: list[WorkflowRun] = field(default_factory=list)

    def worker(self) -> Any:
        return build_temporal_worker(self.env.client, self.activities, task_queue=TASK_QUEUE)

    async def start(self, task_id: uuid.UUID | None = None) -> WorkflowRun:
        run = await self.engine.astart(task_id or uuid.uuid4())
        self._runs.append(run)
        return run

    def handle(self, run: WorkflowRun) -> Any:
        # Prefer the engine's original start handle: only that handle auto-skips
        # time on ``result()`` under the time-skipping env (re-fetched handles do
        # not). Falls back to a re-fetched typed handle when unavailable.
        started = self.engine.workflow_handle(run.id)
        if started is not None:
            return started
        wf_id = run.context["temporal_workflow_id"]
        return self.env.client.get_workflow_handle_for(FeatureWorkflow.run, wf_id)

    async def wait_awaiting(self, run: WorkflowRun, expected: Iterable[str]) -> None:
        expected_set = set(expected)
        handle = self.handle(run)
        last: set[str] = set()
        for _ in range(300):
            last = set(await handle.query(FeatureWorkflow.awaiting))
            if expected_set <= last:
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"workflow never awaited {expected_set}; last={last}")

    async def wait_state(self, run: WorkflowRun, state: str) -> None:
        handle = self.handle(run)
        for _ in range(300):
            if await handle.query(FeatureWorkflow.current_state) == state:
                return
            await asyncio.sleep(0.02)
        raise AssertionError(
            f"workflow never reached {state}; at {self.engine.get_run(run.id).current_state}"
        )

    def transitions(self, run: WorkflowRun) -> list[dict[str, Any]]:
        return self.engine.history(run.id)

    def state_path(self, run: WorkflowRun) -> list[tuple[str, str, str]]:
        return [(t["from_state"], t["to_state"], t["event"]) for t in self.transitions(run)]


@pytest.fixture
def harness(time_skip_env: Any) -> Callable[..., Harness]:
    def _build(**activity_fns: Any) -> Harness:
        store = InMemoryWorkflowStore()
        activities = WorkflowActivities(store=store, **activity_fns)
        workspace_id = uuid.uuid4()
        engine = TemporalWorkflowEngine(
            workspace_id=workspace_id,
            store=store,
            client=time_skip_env.client,
            task_queue=TASK_QUEUE,
        )
        return Harness(
            env=time_skip_env,
            store=store,
            activities=activities,
            engine=engine,
            workspace_id=workspace_id,
        )

    return _build
