"""Temporal integration tier — the durable FeatureWorkflow (F25 §7).

Driven on the time-skipping ``WorkflowEnvironment`` with real Activities over an
in-memory projection store and scriptable service callables.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from forge_contracts import WorkflowState
from forge_workflow.temporal.payloads import (
    AgentRunResultDTO,
    ChecksResult,
    GuardInputs,
    RunChecksInput,
)

pytestmark = pytest.mark.asyncio


def _passing_checks() -> dict[str, bool]:
    return {"lint": True, "type_check": True, "tests": True, "coverage": True}


async def test_full_happy_path_temporal(harness: Any) -> None:
    """AC2/AC3 — created → … → closed; ordered transitions; final state."""
    h = harness()  # defaults: plan not required, agent ok, checks pass
    async with h.worker():
        run = await h.start()

        # AC3 — the first persisted transition is created → spec_drafting (seq 1).
        await h.wait_awaiting(run, {"spec_approved_by_human"})
        first = h.transitions(run)[0]
        assert (first["from_state"], first["to_state"], first["sequence"]) == (
            "created",
            "spec_drafting",
            1,
        )

        state = await h.engine.atransition(run.id, "spec_approved_by_human")
        assert state == WorkflowState.SPEC_APPROVED

        # plan auto-approved (not required) → runs to the merge gate.
        await h.wait_awaiting(run, {"review_approved_by_human"})
        state = await h.engine.atransition(
            run.id, "review_approved_by_human", ci_status_green=True, spec_validated=True
        )
        assert state == WorkflowState.MERGED

        result = await h.handle(run).result()
        assert result.final_state == WorkflowState.CLOSED

    final = h.engine.get_run(run.id)
    assert final.current_state == "closed"
    assert final.context["engine_backend"] == "temporal"

    path = [(f, t) for f, t, _ in h.state_path(run)]
    assert path == [
        ("created", "spec_drafting"),
        ("spec_drafting", "clarification"),
        ("clarification", "spec_review"),
        ("spec_review", "spec_approved"),
        ("spec_approved", "plan_drafting"),
        ("plan_drafting", "plan_review"),
        ("plan_review", "task_generation"),
        ("task_generation", "task_ready"),
        ("task_ready", "executing"),
        ("executing", "verifying"),
        ("verifying", "pr_opened"),
        ("pr_opened", "awaiting_review"),
        ("awaiting_review", "merged"),
        ("merged", "closed"),
    ]


async def test_spec_gate_update_and_invalid_event(harness: Any) -> None:
    """AC5 — a no-rule event from the current gate is rejected (state unchanged)."""
    from forge_workflow.exceptions import InvalidTransitionError

    h = harness()
    async with h.worker():
        run = await h.start()
        await h.wait_awaiting(run, {"spec_approved_by_human", "spec_changes_requested"})

        with pytest.raises(InvalidTransitionError):
            await h.engine.atransition(run.id, "plan_approved_by_human")

        assert h.engine.get_run(run.id).current_state == "spec_review"

        # changes-requested loops back to clarification, then re-enters spec_review.
        state = await h.engine.atransition(run.id, "spec_changes_requested")
        assert state == WorkflowState.CLARIFICATION
        await h.wait_awaiting(run, {"spec_approved_by_human"})
        state = await h.engine.atransition(run.id, "spec_approved_by_human")
        assert state == WorkflowState.SPEC_APPROVED


async def test_plan_gate_conditional(harness: Any) -> None:
    """AC6 — plan gate requires a human only when load_guard_inputs says so."""

    def plan_required(req: Any) -> GuardInputs:
        return GuardInputs(
            plan_required=req.phase == "plan",
            preconditions={
                "repo_target_set": True,
                "policy_loaded": True,
                "skill_profile_set": True,
                "knowledge_synced": True,
            },
        )

    h = harness(guard_inputs_fn=plan_required)
    async with h.worker():
        run = await h.start()
        await h.wait_awaiting(run, {"spec_approved_by_human"})
        await h.engine.atransition(run.id, "spec_approved_by_human")

        # plan IS required -> the workflow waits at plan_review for a human event.
        await h.wait_awaiting(run, {"plan_approved_by_human"})
        assert h.engine.get_run(run.id).current_state == "plan_review"
        state = await h.engine.atransition(run.id, "plan_approved_by_human")
        assert state == WorkflowState.TASK_GENERATION


async def test_retry_backoff_durable_timers(harness: Any) -> None:
    """AC8 — three failing run_checks loop verifying→executing with 30/60/120s
    durable backoff; the fourth failure routes to needs_human_input."""

    def always_fail(_: RunChecksInput) -> ChecksResult:
        return ChecksResult(
            results={"lint": True, "type_check": True, "tests": False, "coverage": True}
        )

    h = harness(run_checks_fn=always_fail)
    async with h.worker():
        run = await h.start()
        await h.wait_awaiting(run, {"spec_approved_by_human"})
        await h.engine.atransition(run.id, "spec_approved_by_human")
        # Awaiting the result fast-forwards the durable backoff timers (time-skip).
        result = await h.handle(run).result()

    assert result.final_state == WorkflowState.NEEDS_HUMAN_INPUT
    retries = [
        t
        for t in h.transitions(run)
        if (t["from_state"], t["to_state"]) == ("verifying", "executing")
    ]
    assert [t["payload"]["backoff_seconds"] for t in retries] == [30, 60, 120]

    exhausted = [t for t in h.transitions(run) if t["to_state"] == "needs_human_input"]
    assert exhausted and exhausted[-1]["from_state"] == "verifying"
    assert h.engine.get_run(run.id).current_state == "needs_human_input"


async def test_agent_awaiting_input_then_resume(harness: Any) -> None:
    """AC9 — agent awaiting_input is a return value; resume continues to closed."""
    calls = {"n": 0}

    def agent(_: Any) -> AgentRunResultDTO:
        calls["n"] += 1
        if calls["n"] == 1:
            return AgentRunResultDTO(
                agent_run_id=uuid.uuid4(),
                status="awaiting_input",
                confidence=0.1,
                needs_human_reason="low confidence",
            )
        return AgentRunResultDTO(
            agent_run_id=uuid.uuid4(), status="succeeded", confidence=1.0, checks=_passing_checks()
        )

    h = harness(run_agent_fn=agent, resume_agent_fn=lambda _: AgentRunResultDTO(
        agent_run_id=uuid.uuid4(), status="succeeded", confidence=1.0, checks=_passing_checks()
    ))
    async with h.worker():
        run = await h.start()
        await h.wait_awaiting(run, {"spec_approved_by_human"})
        await h.engine.atransition(run.id, "spec_approved_by_human")
        # agent returns awaiting_input → needs_human_input, awaiting resume.
        await h.wait_awaiting(run, {"resume"})
        assert h.engine.get_run(run.id).current_state == "needs_human_input"
        await h.engine.atransition(run.id, "resume")
        await h.wait_awaiting(run, {"review_approved_by_human"})
        await h.engine.atransition(
            run.id, "review_approved_by_human", ci_status_green=True, spec_validated=True
        )
        result = await h.handle(run).result()
    assert result.final_state == WorkflowState.CLOSED


async def test_cancel_signal_runs_cleanup(harness: Any) -> None:
    """AC16 — cancel Signal from a gate → cleanup_worktree + cancelled transition."""
    cleanup_calls = {"n": 0}

    def cleanup(_: Any) -> bool:
        cleanup_calls["n"] += 1
        return True

    h = harness(cleanup_fn=cleanup)
    async with h.worker():
        run = await h.start()
        await h.wait_awaiting(run, {"spec_approved_by_human"})
        await h.handle(run).signal("cancel_run", "operator cancel")
        result = await h.handle(run).result()

    assert result.final_state == WorkflowState.CANCELLED
    assert cleanup_calls["n"] == 1
    last = h.transitions(run)[-1]
    assert last["to_state"] == "cancelled" and last["event"] == "cancel"
    assert "cleanup_worktree" in last["effects_dispatched"]
    assert h.engine.get_run(run.id).current_state == "cancelled"


async def test_merge_gate_guard_failure(harness: Any) -> None:
    """AC12 — review with incomplete signals fails the merge_ready guard (409)."""
    from forge_workflow.exceptions import GuardFailedError

    h = harness()
    async with h.worker():
        run = await h.start()
        await h.wait_awaiting(run, {"spec_approved_by_human"})
        await h.engine.atransition(run.id, "spec_approved_by_human")
        await h.wait_awaiting(run, {"review_approved_by_human"})

        with pytest.raises(GuardFailedError):
            await h.engine.atransition(
                run.id, "review_approved_by_human", ci_status_green=False, spec_validated=True
            )
        assert h.engine.get_run(run.id).current_state == "awaiting_review"

        # a corrected approval (all signals) merges + closes.
        await h.engine.atransition(
            run.id, "review_approved_by_human", ci_status_green=True, spec_validated=True
        )
        result = await h.handle(run).result()
    assert result.final_state == WorkflowState.CLOSED


async def test_open_pr_idempotent_on_activity_retry(harness: Any) -> None:
    """AC11 — a forced open_pr failure+retry opens exactly one PR (idempotency)."""
    seen: set[str] = set()
    opens = {"n": 0}

    def open_pr(inp: Any) -> Any:
        from forge_workflow.temporal.payloads import OpenPrResult

        if inp.idempotency_key not in seen:
            seen.add(inp.idempotency_key)
            raise RuntimeError("transient open_pr failure")  # forces a retry
        opens["n"] += 1
        return OpenPrResult(pr_number=1)

    h = harness(open_pr_fn=open_pr)
    async with h.worker():
        run = await h.start()
        await h.wait_awaiting(run, {"spec_approved_by_human"})
        await h.engine.atransition(run.id, "spec_approved_by_human")
        await h.wait_awaiting(run, {"review_approved_by_human"})
        await h.engine.atransition(
            run.id, "review_approved_by_human", ci_status_green=True, spec_validated=True
        )
        await h.handle(run).result()

    # The activity ran twice (1 failure + 1 success) but only one PR was opened.
    assert opens["n"] == 1
    pr_opened = [t for t in h.transitions(run) if t["to_state"] == "pr_opened"]
    assert len(pr_opened) == 1
