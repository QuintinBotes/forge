"""Workflow-replay determinism (F25 AC15).

A recorded happy-path history must replay cleanly through ``Replayer`` (proving
the workflow body uses only deterministic constructs). A canary proves the test
actually *catches* non-determinism: replaying that history against a structurally
different workflow registered under the same name raises a non-determinism error.
"""

from __future__ import annotations

from typing import Any

import pytest
from temporalio import workflow
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Replayer

from forge_workflow.temporal.payloads import WorkflowResult
from forge_workflow.temporal.worker import feature_workflow_runner
from forge_workflow.temporal.workflows import FeatureWorkflow

pytestmark = pytest.mark.asyncio


async def _record_happy_path_history(harness: Any) -> Any:
    h = harness()
    async with h.worker():
        run = await h.start()
        await h.wait_awaiting(run, {"spec_approved_by_human"})
        await h.engine.atransition(run.id, "spec_approved_by_human")
        await h.wait_awaiting(run, {"review_approved_by_human"})
        await h.engine.atransition(
            run.id, "review_approved_by_human", ci_status_green=True, spec_validated=True
        )
        await h.handle(run).result()
        return await h.handle(run).fetch_history()


async def test_happy_path_replays_deterministically(harness: Any) -> None:
    history = await _record_happy_path_history(harness)
    replayer = Replayer(
        workflows=[FeatureWorkflow],
        workflow_runner=feature_workflow_runner(),
        data_converter=pydantic_data_converter,
    )
    # Raises on any non-determinism; clean replay == deterministic workflow body.
    await replayer.replay_workflow(history)


@workflow.defn(name="forge.FeatureWorkflow")
class _DivergentWorkflow:
    """Same workflow name, structurally different body — a non-determinism canary."""

    @workflow.run
    async def run(self, params: Any) -> WorkflowResult:  # pragma: no cover - replay only
        from forge_contracts import WorkflowState

        return WorkflowResult(final_state=WorkflowState.CREATED, transition_count=0)

    @workflow.update
    async def submit_event(self, event: Any) -> str:  # pragma: no cover
        return "created"

    @workflow.signal
    async def cancel_run(self, reason: str = "cancelled") -> None:  # pragma: no cover
        return None

    @workflow.query
    def awaiting(self) -> list[str]:  # pragma: no cover
        return []

    @workflow.query
    def current_state(self) -> str:  # pragma: no cover
        return "created"

    @workflow.query
    def transition_count(self) -> int:  # pragma: no cover
        return 0


async def test_replay_detects_nondeterminism_canary(harness: Any) -> None:
    history = await _record_happy_path_history(harness)
    replayer = Replayer(
        workflows=[_DivergentWorkflow],
        workflow_runner=feature_workflow_runner(),
        data_converter=pydantic_data_converter,
    )
    with pytest.raises(Exception):  # noqa: B017 - any replay failure proves detection
        await replayer.replay_workflow(history)
