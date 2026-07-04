"""Cross-engine parity (F25 AC2).

One scenario script driven against **both** engines (V1 ``WorkflowEngineImpl`` FSM
and V2 ``TemporalWorkflowEngine``) traverses the identical ``created → … → closed``
state path. This is the contract that lets either engine ship behind the frozen
``WorkflowEngine`` protocol.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from forge_workflow.engine import WorkflowEngineImpl

pytestmark = pytest.mark.asyncio

# Canonical happy-path state sequence (the acceptance bar both engines meet).
CANONICAL_STATES = [
    "created",
    "spec_drafting",
    "clarification",
    "spec_review",
    "spec_approved",
    "plan_drafting",
    "plan_review",
    "task_generation",
    "task_ready",
    "executing",
    "verifying",
    "pr_opened",
    "awaiting_review",
    "merged",
    "closed",
]

# Ordered events that drive the V1 FSM through the same path.
_FSM_EVENTS = [
    "generate_spec_draft",
    "gather_clarifications",
    "submit_spec_for_review",
    "spec_approved_by_human",
    "generate_plan",
    "submit_plan_for_review",
    "plan_approved_by_human",
    "generate_tasks",
    "start_agent_run",
    "run_checks",
    "all_checks_passed",
    "request_reviews",
    "review_approved_by_human",
    "close_task",
]


def _fsm_state_path() -> list[str]:
    engine = WorkflowEngineImpl()
    run = engine.start(uuid.uuid4())
    # The merge gate AND-guard reads ci/spec signals from the run context.
    engine.update_context(run.id, {"ci_status_green": True, "spec_validated": True})
    states = [run.current_state]
    for event in _FSM_EVENTS:
        engine.transition(run.id, event)
        states.append(engine.get_run(run.id).current_state)
    return states


async def _temporal_state_path(harness: Any) -> list[str]:
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
    # Reconstruct the visited states from the projection (created + each to_state).
    transitions = h.transitions(run)
    return [transitions[0]["from_state"], *[t["to_state"] for t in transitions]]


async def test_both_engines_traverse_identical_state_path(harness: Any) -> None:
    fsm_states = _fsm_state_path()
    temporal_states = await _temporal_state_path(harness)

    assert fsm_states == CANONICAL_STATES
    assert temporal_states == CANONICAL_STATES
    assert fsm_states == temporal_states
