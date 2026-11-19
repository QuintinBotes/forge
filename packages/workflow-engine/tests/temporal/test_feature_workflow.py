"""Temporal integration tier — the durable FeatureWorkflow (F25 §7).

Driven on the time-skipping ``WorkflowEnvironment`` with real Activities over an
in-memory projection store and scriptable service callables.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from scf_contracts import WorkflowState
from scf_workflow.temporal.payloads import (
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
    from scf_workflow.exceptions import InvalidTransitionError

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

    h = harness(
        run_agent_fn=agent,
        resume_agent_fn=lambda _: AgentRunResultDTO(
            agent_run_id=uuid.uuid4(), status="succeeded", confidence=1.0, checks=_passing_checks()
        ),
    )
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
    """AC12, #280 criterion 2: the merge guard reads CI itself. A caller that says CI
    is green does not make it so; the run stays at review until SCF reads it green."""
    from scf_workflow.exceptions import GuardFailedError
    from scf_workflow.temporal.activities import _default_guard_inputs
    from scf_workflow.temporal.payloads import GuardInputs

    ci = {"green": False, "reads": 0}

    def guard(req: Any) -> GuardInputs:
        if req.phase != "merge":
            return _default_guard_inputs(req)
        ci["reads"] += 1
        return GuardInputs(plan_required=False, ci_status_green=ci["green"], spec_validated=True)

    h = harness(guard_inputs_fn=guard)
    async with h.worker():
        run = await h.start()
        await h.wait_awaiting(run, {"spec_approved_by_human"})
        await h.engine.atransition(run.id, "spec_approved_by_human")
        await h.wait_awaiting(run, {"review_approved_by_human"})

        with pytest.raises(GuardFailedError):
            await h.engine.atransition(
                run.id, "review_approved_by_human", ci_status_green=True, spec_validated=True
            )
        assert h.engine.get_run(run.id).current_state == "awaiting_review"

        # Once SCF reads CI green, the same approval (no payload) merges and closes.
        ci["green"] = True
        await h.engine.atransition(run.id, "review_approved_by_human")
        result = await h.handle(run).result()
    assert result.final_state == WorkflowState.CLOSED
    assert ci["reads"] == 2, "each approval reads CI again"


async def test_open_pr_idempotent_on_activity_retry(harness: Any) -> None:
    """AC11 — a forced open_pr failure+retry opens exactly one PR (idempotency)."""
    seen: set[str] = set()
    opens = {"n": 0}

    def open_pr(inp: Any) -> Any:
        from scf_workflow.temporal.payloads import OpenPrResult

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


async def test_a_run_started_for_someone_names_them_once_and_waits_at_the_spec_gate(
    harness: Any,
) -> None:
    """#171: an issue-triggered run records who started it on its first transition,
    and never as an approver; it waits at the spec gate and runs no agent first."""
    agent_calls: list[Any] = []

    def agent(inp: Any) -> AgentRunResultDTO:
        agent_calls.append(inp)
        return AgentRunResultDTO(agent_run_id=uuid.uuid4(), status="succeeded")

    h = harness(run_agent_fn=agent)
    async with h.worker():
        run = await h.engine.astart(uuid.uuid4(), started_by="github:octocat")
        await h.wait_awaiting(run, {"spec_approved_by_human"})

        transitions = h.transitions(run)
        assert (transitions[0]["event"], transitions[0]["actor"]) == (
            "generate_spec_draft",
            "github:octocat",
        )
        assert all(t["actor"] != "github:octocat" for t in transitions[1:])
        assert h.engine.get_run(run.id).current_state == "spec_review"
        assert agent_calls == [], "no agent before a person decides the spec gate"


async def test_the_first_agent_run_takes_the_id_its_starter_was_given(harness: Any) -> None:
    """#276: ``POST /agent/runs`` returns ``agent_run_id(run)`` before the agent runs;
    the agent step's key derives that id (``forge.run_agent`` takes uuid5 of it)."""
    from scf_workflow.temporal.ids import agent_run_id

    calls: list[Any] = []

    def agent(inp: Any) -> AgentRunResultDTO:
        calls.append(inp)
        return AgentRunResultDTO(agent_run_id=uuid.uuid4(), status="succeeded")

    h = harness(run_agent_fn=agent)
    async with h.worker():
        run = await h.start()
        await h.wait_awaiting(run, {"spec_approved_by_human"})
        await h.engine.atransition(run.id, "spec_approved_by_human")
        await h.wait_awaiting(run, {"review_approved_by_human"})

    first = calls[0]
    assert uuid.uuid5(first.workflow_run_id, first.idempotency_key) == agent_run_id(run.id)


async def test_the_check_activity_takes_its_configured_limit_and_heartbeats(
    harness: Any,
) -> None:
    """#172 criterion 8: a whole test suite needs more than two minutes; the limit is
    configured (30 minutes by default) and the activity heartbeats."""
    from scf_workflow.temporal.config import TemporalSettings
    from scf_workflow.temporal.engine import TemporalWorkflowEngine

    assert TemporalSettings().checks_activity_timeout >= 1800
    h = harness()
    engine = TemporalWorkflowEngine(
        workspace_id=h.workspace_id,
        store=h.store,
        client=h.env.client,
        task_queue=h.engine._task_queue,
        checks_timeout_seconds=2400,
    )
    async with h.worker():
        run = await engine.astart(uuid.uuid4())
        await h.wait_awaiting(run, {"spec_approved_by_human"})
        await engine.atransition(run.id, "spec_approved_by_human")
        await h.wait_awaiting(run, {"review_approved_by_human"})
        history = await engine.workflow_handle(run.id).fetch_history()  # type: ignore[union-attr]

    [scheduled] = [
        e.activity_task_scheduled_event_attributes
        for e in history.events
        if e.HasField("activity_task_scheduled_event_attributes")
        and e.activity_task_scheduled_event_attributes.activity_type.name == "forge.run_checks"
    ]
    assert scheduled.start_to_close_timeout.seconds == 2400
    assert scheduled.heartbeat_timeout.seconds > 0


async def test_a_change_no_reviewer_can_judge_pauses_for_a_person_without_a_pr(
    harness: Any,
) -> None:
    """#173 criterion 4: with no usable independent reviewer, the run moves to
    needs_human_input with the reason, and no pull request is opened."""
    opened: list[Any] = []

    def no_reviewer(_: RunChecksInput) -> ChecksResult:
        return ChecksResult(results={"tests": True}, pause_reason="reviewer_not_configured")

    h = harness(run_checks_fn=no_reviewer, open_pr_fn=opened.append)
    async with h.worker():
        run = await h.start()
        await h.wait_awaiting(run, {"spec_approved_by_human"})
        await h.engine.atransition(run.id, "spec_approved_by_human")
        result = await h.handle(run).result()

    assert result.final_state == WorkflowState.NEEDS_HUMAN_INPUT
    assert result.failure_reason == "reviewer_not_configured"
    paused = [t for t in h.transitions(run) if t["to_state"] == "needs_human_input"]
    assert [t["payload"]["reason"] for t in paused] == ["reviewer_not_configured"]
    assert opened == []


async def test_an_activity_that_refuses_records_the_run_failed_with_why(harness: Any) -> None:
    """#278: a non-retryable activity error still fails the execution, and the run
    records ``failed`` with the reason first, so the issue it came from hears it."""
    from temporalio.client import WorkflowFailureError
    from temporalio.exceptions import ApplicationError

    def nothing_to_deliver(_: Any) -> Any:
        raise ApplicationError(
            "scf/run has no commits ahead of main", type="NoChangesToDeliver", non_retryable=True
        )

    h = harness(open_pr_fn=nothing_to_deliver)
    async with h.worker():
        run = await h.start()
        await h.wait_awaiting(run, {"spec_approved_by_human"})
        await h.engine.atransition(run.id, "spec_approved_by_human")
        with pytest.raises(WorkflowFailureError):
            await h.handle(run).result()

    final = h.engine.get_run(run.id)
    assert (final.current_state, final.status.value) == ("failed", "failed")
    [last] = h.transitions(run)[-1:]
    assert (last["from_state"], last["to_state"]) == ("pr_opened", "failed")
    assert last["payload"]["reason"] == ("NoChangesToDeliver: scf/run has no commits ahead of main")


async def test_held_out_scenarios_are_drafted_before_the_spec_gate_opens(harness: Any) -> None:
    """#258 criterion 1: after the spec is submitted for review and before the `spec`
    approval request is created, the drafting step runs once."""
    import uuid as _uuid

    from scf_workflow.temporal.payloads import (
        ApprovalInput,
        DraftScenariosInput,
        DraftScenariosResult,
    )

    calls: list[str] = []

    def draft(inp: DraftScenariosInput) -> DraftScenariosResult:
        calls.append("draft")
        return DraftScenariosResult(scenario_ids=["s-1"])

    def approval(inp: ApprovalInput) -> _uuid.UUID:
        calls.append(f"approval:{inp.gate}")
        return _uuid.uuid4()

    h = harness(draft_scenarios_fn=draft, approval_fn=approval)
    async with h.worker():
        run = await h.start()
        await h.wait_awaiting(run, {"spec_approved_by_human"})

    assert calls == ["draft", "approval:spec"]
    states = [t["to_state"] for t in h.transitions(run)]
    assert states[-1] == "spec_review"


async def test_the_verifier_drafts_again_each_time_the_spec_returns_for_changes(
    harness: Any,
) -> None:
    """#288 criterion 3: after the spec gate returns changes and before it opens again,
    the drafting step runs with a key of its own (it withdraws the earlier drafts)."""
    import uuid as _uuid

    from scf_workflow.temporal.payloads import (
        ApprovalInput,
        DraftScenariosInput,
        DraftScenariosResult,
    )

    calls: list[str] = []
    keys: list[str] = []

    def draft(inp: DraftScenariosInput) -> DraftScenariosResult:
        calls.append("draft")
        keys.append(inp.idempotency_key)
        return DraftScenariosResult(scenario_ids=[f"s-{len(keys)}"])

    def approval(inp: ApprovalInput) -> _uuid.UUID:
        calls.append(f"approval:{inp.gate}")
        return _uuid.uuid4()

    h = harness(draft_scenarios_fn=draft, approval_fn=approval)
    async with h.worker():
        run = await h.start()
        for _ in range(2):
            await h.wait_awaiting(run, {"spec_approved_by_human"})
            await h.engine.atransition(run.id, "spec_changes_requested")
        await h.wait_awaiting(run, {"spec_approved_by_human"})

    assert calls == ["draft", "approval:spec"] * 3
    assert keys[0].endswith(":draft_held_out:0") and len(set(keys)) == 3
