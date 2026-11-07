"""Unwired Activities refuse instead of reporting results they did not produce (ADR-0007)."""

from __future__ import annotations

import uuid

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from forge_workflow.temporal.activities import WorkflowActivities
from forge_workflow.temporal.payloads import (
    AgentRunResultDTO,
    OpenPrInput,
    OpenPrResult,
    RunAgentInput,
    RunChecksInput,
)

RUN = uuid.uuid4()
WS = uuid.uuid4()


def _agent_input() -> RunAgentInput:
    return RunAgentInput(
        workflow_run_id=RUN, task_id=uuid.uuid4(), workspace_id=WS, attempt=1, idempotency_key="k"
    )


def _pr_input() -> OpenPrInput:
    return OpenPrInput(workflow_run_id=RUN, workspace_id=WS, branch_name="b", idempotency_key="k")


async def test_unwired_agent_activity_refuses_non_retryably() -> None:
    activities = WorkflowActivities()
    with pytest.raises(ApplicationError) as caught:
        await ActivityEnvironment().run(activities.run_agent, _agent_input())
    assert caught.value.type == "ActivityNotConfigured"
    assert caught.value.non_retryable is True
    assert "forge.run_agent" in str(caught.value)


@pytest.mark.parametrize(
    ("method", "payload"),
    [
        (
            "run_checks",
            RunChecksInput(workflow_run_id=RUN, workspace_id=WS, attempt=1, idempotency_key="k"),
        ),
        ("open_pr_with_spec_traceability", _pr_input()),
    ],
)
async def test_unwired_effects_refuse(method: str, payload: object) -> None:
    activities = WorkflowActivities()
    with pytest.raises(ApplicationError, match="not configured"):
        await ActivityEnvironment().run(getattr(activities, method), payload)


async def test_fake_pr_stand_in_never_invents_a_pr() -> None:
    activities = WorkflowActivities(allow_fakes=True)
    result = await ActivityEnvironment().run(activities.open_pr_with_spec_traceability, _pr_input())
    assert result == OpenPrResult(pr_number=None, pr_url=None)


async def test_injected_service_is_used_without_fakes() -> None:
    expected = AgentRunResultDTO(
        agent_run_id=uuid.uuid4(),
        status="succeeded",
        confidence=0.9,
        checks={"tests": True},
        branch_name="forge/x",
        head_commit_sha="abc",
    )
    activities = WorkflowActivities(run_agent_fn=lambda _: expected)
    assert await ActivityEnvironment().run(activities.run_agent, _agent_input()) == expected
