"""Red-Team Gate integration tier — the adversarial scan wired into
``FeatureWorkflow.run`` between ``submit_spec_for_review`` and the human spec
gate (Red-Team Gate, slice wire-gate).

Driven on the time-skipping ``WorkflowEnvironment`` with real Activities over an
in-memory projection store and a scriptable ``red_team_fn``. The default
parked-pass must leave the existing flow byte-identical; a survive proceeds to
the human gate; a block routes the change back for changes (→ clarification →
spec_review) before any human sees it.

The DB side of a survive (persisting the ``RedTeamRecord`` + chaining the
``redteam.survived`` audit event) is exercised directly in
``packages/db/tests/test_red_team_models.py`` — here the injected ``red_team_fn``
stands in for the recorder so the durable routing can be asserted offline.
"""

from __future__ import annotations

from typing import Any

import pytest

from forge_contracts import WorkflowState
from forge_workflow.temporal.payloads import (
    REDTEAM_BLOCKED,
    REDTEAM_SURVIVED,
    RedTeamInput,
    RedTeamResult,
)

pytestmark = pytest.mark.asyncio


async def test_default_parked_pass_keeps_existing_flow(harness: Any) -> None:
    """With no adversary wired, the default parked-pass survive leaves the flow
    unchanged: no red-team-driven transition, straight to the human spec gate."""
    h = harness()  # defaults: parked-pass red-team scan
    async with h.worker():
        run = await h.start()
        await h.wait_awaiting(run, {"spec_approved_by_human", "spec_changes_requested"})

        # No red-team-driven changes-requested transition was recorded.
        assert [t for t in h.transitions(run) if t["actor"] == "red-team"] == []

        # The spec path is exactly the unmodified lifecycle prefix.
        path = [(f, t) for f, t, _ in h.state_path(run)]
        assert path[:3] == [
            ("created", "spec_drafting"),
            ("spec_drafting", "clarification"),
            ("clarification", "spec_review"),
        ]

        # Proceeds to close unchanged.
        await h.engine.atransition(run.id, "spec_approved_by_human")
        await h.wait_awaiting(run, {"review_approved_by_human"})
        await h.engine.atransition(
            run.id, "review_approved_by_human", ci_status_green=True, spec_validated=True
        )
        result = await h.handle(run).result()
    assert result.final_state == WorkflowState.CLOSED


async def test_red_team_survive_records_and_proceeds(harness: Any) -> None:
    """A survive verdict runs the scan (once, with the run's idempotency key) and
    proceeds to the human spec gate with no changes-requested detour."""
    calls: list[RedTeamInput] = []

    def survive(inp: RedTeamInput) -> RedTeamResult:
        calls.append(inp)
        return RedTeamResult(
            verdict=REDTEAM_SURVIVED,
            kind="failing_test",
            evidence={"ran": True, "failed": False},
            adversary_model="gpt-5-heavy",
            coder_model="claude-sonnet-4",
        )

    h = harness(red_team_fn=survive)
    async with h.worker():
        run = await h.start()
        await h.wait_awaiting(run, {"spec_approved_by_human", "spec_changes_requested"})

        # The scan ran exactly once, idempotency-keyed on the run + phase.
        assert len(calls) == 1
        assert calls[0].workflow_run_id == run.id
        assert calls[0].phase == "spec"
        assert calls[0].idempotency_key == f"{run.id}:red_team:spec"

        # A survive does NOT route back for changes.
        assert [t for t in h.transitions(run) if t["actor"] == "red-team"] == []
        assert h.engine.get_run(run.id).current_state == "spec_review"

        await h.engine.atransition(run.id, "spec_approved_by_human")
        await h.wait_awaiting(run, {"review_approved_by_human"})
        await h.engine.atransition(
            run.id, "review_approved_by_human", ci_status_green=True, spec_validated=True
        )
        result = await h.handle(run).result()
    assert result.final_state == WorkflowState.CLOSED


async def test_red_team_block_routes_to_changes_requested(harness: Any) -> None:
    """A block verdict routes the candidate back for changes (spec_review →
    clarification via a red-team-actor ``spec_changes_requested``), re-enters
    spec_review, and lands at the human gate carrying the adversary's evidence."""

    def block(inp: RedTeamInput) -> RedTeamResult:
        return RedTeamResult(
            verdict=REDTEAM_BLOCKED,
            kind="failing_test",
            evidence={"test": "test_boom", "stdout": "AssertionError"},
            adversary_model="gpt-5-heavy",
            coder_model="claude-sonnet-4",
        )

    h = harness(red_team_fn=block)
    async with h.worker():
        run = await h.start()
        await h.wait_awaiting(run, {"spec_approved_by_human", "spec_changes_requested"})

        # Exactly one red-team-driven changes-requested transition, spec_review →
        # clarification, carrying the adversary's structured evidence.
        rt = [
            t
            for t in h.transitions(run)
            if t["event"] == "spec_changes_requested" and t["actor"] == "red-team"
        ]
        assert len(rt) == 1
        assert (rt[0]["from_state"], rt[0]["to_state"]) == ("spec_review", "clarification")
        assert rt[0]["payload"]["red_team"] is True
        assert rt[0]["payload"]["kind"] == "failing_test"
        assert rt[0]["payload"]["evidence"]["stdout"] == "AssertionError"

        # It re-entered spec_review and is awaiting the human gate (a human must
        # now address the finding).
        assert h.engine.get_run(run.id).current_state == "spec_review"

        # A human can still drive it forward after addressing the block.
        await h.engine.atransition(run.id, "spec_approved_by_human")
        assert h.engine.get_run(run.id).current_state == "spec_approved"
