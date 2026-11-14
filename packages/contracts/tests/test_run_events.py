"""ADR-0013: the run-event vocabulary and its gated/observed honesty."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from forge_contracts import (
    ENFORCEMENT,
    Decision,
    DecisionEffect,
    RunEvent,
    RunEventDraft,
    RunEventType,
    Step,
    StepKind,
    ToolCall,
    events_for_step,
)

NOW = datetime(2026, 11, 8, tzinfo=UTC)


def test_every_event_type_has_exactly_one_enforcement() -> None:
    assert set(ENFORCEMENT) == set(RunEventType)
    assert set(ENFORCEMENT.values()) <= {"gated", "observed"}


@pytest.mark.parametrize("event_type", list(RunEventType))
def test_an_event_cannot_claim_enforcement_it_did_not_have(event_type: RunEventType) -> None:
    honest = ENFORCEMENT[event_type]
    dishonest = "observed" if honest == "gated" else "gated"
    with pytest.raises(ValueError):
        RunEvent(
            run_id=uuid.uuid4(),
            seq=1,
            type=event_type,
            ts=NOW,
            summary="x",
            enforcement=dishonest,
            enforceable=dishonest == "gated",
        )
    event = RunEvent.from_draft(
        RunEventDraft(type=event_type, summary="x"), run_id=uuid.uuid4(), seq=1, ts=NOW
    )
    assert event.enforceable is (honest == "gated")
    assert event.observed_by == "forge"


def _call(tool: str = "run_tests") -> ToolCall:
    return ToolCall(tool=tool, action="execute", arguments={"x": 1})


@pytest.mark.parametrize(
    ("effect", "permission"),
    [
        (DecisionEffect.ALLOW, RunEventType.PERMISSION_GRANTED),
        (DecisionEffect.DENY, RunEventType.PERMISSION_DENIED),
        (DecisionEffect.REQUIRES_APPROVAL, RunEventType.PERMISSION_REQUESTED),
    ],
)
def test_a_decision_is_an_observed_request_then_a_gated_permission(
    effect: DecisionEffect, permission: RunEventType
) -> None:
    step = Step(
        kind=StepKind.DECISION,
        tool_call=_call(),
        decision=Decision(effect=effect, matched_rule="r1", reason="because"),
    )
    drafts = events_for_step(step)
    assert [d.type for d in drafts] == [RunEventType.TOOL_REQUESTED, permission]
    assert [d.enforcement for d in drafts] == ["observed", "gated"]
    assert drafts[1].payload["authority"] == "forge_policy"


def test_a_tool_call_is_gated_and_a_commit_also_changes_git() -> None:
    ran = events_for_step(Step(kind=StepKind.TOOL_CALL, tool_call=_call(), output="3 passed"))
    assert [d.type for d in ran] == [RunEventType.TOOL_COMPLETED]
    committed = events_for_step(
        Step(kind=StepKind.TOOL_CALL, tool_call=_call("git_commit"), output="abc123 add search")
    )
    assert [d.type for d in committed] == [RunEventType.TOOL_COMPLETED, RunEventType.GIT_CHANGED]
    assert {d.enforcement for d in committed} == {"gated"}


def test_model_text_is_observed_and_tool_observations_fold_into_their_call() -> None:
    message = events_for_step(Step(kind=StepKind.PLAN, thought="read the failing test first"))
    assert [(d.type, d.enforcement) for d in message] == [(RunEventType.AGENT_MESSAGE, "observed")]
    assert (
        events_for_step(Step(kind=StepKind.OBSERVATION, tool_call=_call(), observation="3 passed"))
        == []
    )
    assert events_for_step(Step(kind=StepKind.MESSAGE)) == []
