"""Frozen ``FeatureWorkflow`` replay (spec K P2, step 0).

``fixtures/feature_happy_path.json`` is a happy-path history recorded with the
time-skipping harness *before* the P2 seams landed. Replaying it with today's
``FeatureWorkflow`` proves the payload additions and engine changes kept the
workflow's command order (and old payloads still decode). The canary proves a
command-order change is caught.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from temporalio import workflow
from temporalio.client import WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Replayer

from forge_workflow.temporal.payloads import WorkflowParams, WorkflowResult
from forge_workflow.temporal.worker import feature_workflow_runner
from forge_workflow.temporal.workflows import FeatureWorkflow

pytestmark = pytest.mark.asyncio

_FIXTURES = Path(__file__).parent / "fixtures"
_FIXTURE = _FIXTURES / "feature_happy_path.json"
#: A cancel recorded while ``cancel_run`` took only a reason (the actor came later).
_CANCEL = _FIXTURES / "feature_cancel_signal.json"


def _history() -> WorkflowHistory:
    return WorkflowHistory.from_json("wf-frozen-feature-happy-path", _FIXTURE.read_text())


def _replayer() -> Replayer:
    return Replayer(
        workflows=[FeatureWorkflow],
        workflow_runner=feature_workflow_runner(),
        data_converter=pydantic_data_converter,
    )


async def test_frozen_feature_happy_path_replays() -> None:
    await _replayer().replay_workflow(_history())


async def test_a_one_argument_cancel_history_replays() -> None:
    history = WorkflowHistory.from_json("wf-frozen-feature-cancel", _CANCEL.read_text())
    await _replayer().replay_workflow(history)


@workflow.defn(name="forge.FeatureWorkflow")
class _ReorderedWorkflow:
    """Same name, one activity before the first persist: a command-order change."""

    @workflow.run
    async def run(self, params: WorkflowParams) -> WorkflowResult:  # pragma: no cover
        from datetime import timedelta

        from forge_contracts import WorkflowState

        await workflow.execute_activity(
            "forge.pause_and_notify", arg=None, start_to_close_timeout=timedelta(seconds=5)
        )
        return WorkflowResult(final_state=WorkflowState.CREATED, transition_count=0)

    @workflow.update
    async def submit_event(self, event: Any) -> str:  # pragma: no cover
        return "created"

    @workflow.signal
    async def cancel_run(self, reason: str = "cancelled") -> None:  # pragma: no cover
        return None


async def test_frozen_replay_catches_a_command_order_change() -> None:
    replayer = Replayer(
        workflows=[_ReorderedWorkflow],
        workflow_runner=feature_workflow_runner(),
        data_converter=pydantic_data_converter,
    )
    with pytest.raises(workflow.NondeterminismError):
        await replayer.replay_workflow(_history())
