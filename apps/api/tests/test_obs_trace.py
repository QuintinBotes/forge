"""Tests for the run-trace assembler (Task 1.14 — observability + audit).

Spec Observability: "Run trace viewer with step-level inspection" and
"Replayable workflow runs with step-level inspection". The assembler turns an
agent run's recorded steps into an ordered, redacted, summarised trace.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from forge_api.observability.trace import RunTrace, RunTraceAssembler
from forge_contracts import AgentRunResult, Step, ToolCall
from forge_contracts.enums import RunStatus, StepKind


def test_assemble_orders_steps_by_index_and_reindexes() -> None:
    steps = [
        Step(index=2, kind=StepKind.OBSERVATION, observation="saw output"),
        Step(index=0, kind=StepKind.PLAN, thought="make a plan"),
        Step(index=1, kind=StepKind.TOOL_CALL, tool_call=ToolCall(tool="write_file")),
    ]
    trace = RunTraceAssembler().assemble(uuid.uuid4(), steps)
    assert isinstance(trace, RunTrace)
    assert [s.index for s in trace.steps] == [0, 1, 2]
    assert trace.steps[0].kind is StepKind.PLAN
    assert trace.steps[1].kind is StepKind.TOOL_CALL
    assert trace.steps[2].kind is StepKind.OBSERVATION


def test_assemble_orders_by_timestamp_when_indices_absent() -> None:
    base = datetime(2026, 6, 26, 12, 0, 0, tzinfo=UTC)
    steps = [
        Step(kind=StepKind.OBSERVATION, observation="third", timestamp=base + timedelta(seconds=2)),
        Step(kind=StepKind.PLAN, thought="first", timestamp=base),
        Step(kind=StepKind.MESSAGE, output="second", timestamp=base + timedelta(seconds=1)),
    ]
    trace = RunTraceAssembler().assemble(uuid.uuid4(), steps)
    assert [s.thought or s.output or s.observation for s in trace.steps] == [
        "first",
        "second",
        "third",
    ]


def test_assemble_counts_steps_by_kind_and_sums_duration() -> None:
    steps = [
        Step(index=0, kind=StepKind.PLAN, duration_ms=10),
        Step(index=1, kind=StepKind.TOOL_CALL, duration_ms=20),
        Step(index=2, kind=StepKind.TOOL_CALL, duration_ms=30),
    ]
    trace = RunTraceAssembler().assemble(uuid.uuid4(), steps)
    assert trace.total_steps == 3
    assert trace.step_counts["tool_call"] == 2
    assert trace.step_counts["plan"] == 1
    assert trace.total_duration_ms == 60


def test_assemble_redacts_secrets_in_step_text() -> None:
    steps = [
        Step(index=0, kind=StepKind.OBSERVATION, observation="got Bearer abcDEF123456ghiJKL back"),
    ]
    trace = RunTraceAssembler().assemble(uuid.uuid4(), steps)
    assert "abcDEF123456ghiJKL" not in (trace.steps[0].observation or "")


def test_assemble_can_skip_redaction_when_disabled() -> None:
    steps = [Step(index=0, kind=StepKind.OBSERVATION, observation="Bearer abcDEF123456ghiJKL")]
    trace = RunTraceAssembler(redact=False).assemble(uuid.uuid4(), steps)
    assert "abcDEF123456ghiJKL" in (trace.steps[0].observation or "")


def test_from_agent_result_propagates_run_metadata() -> None:
    run_id = uuid.uuid4()
    result = AgentRunResult(
        run_id=run_id,
        status=RunStatus.SUCCEEDED,
        confidence=0.91,
        steps=[
            Step(index=0, kind=StepKind.PLAN, thought="plan"),
            Step(index=1, kind=StepKind.OUTPUT, output="done"),
        ],
    )
    trace = RunTraceAssembler().from_agent_result(result)
    assert trace.run_id == run_id
    assert trace.status is RunStatus.SUCCEEDED
    assert trace.confidence == 0.91
    assert trace.total_steps == 2


def test_from_steps_coerces_raw_dicts() -> None:
    run_id = uuid.uuid4()
    raw = [
        {"index": 0, "kind": "plan", "thought": "p"},
        {"index": 1, "kind": "output", "output": "o"},
    ]
    trace = RunTraceAssembler().from_steps(run_id, raw)
    assert trace.total_steps == 2
    assert trace.steps[0].kind is StepKind.PLAN


def test_assemble_merges_subagent_steps_and_flags_them() -> None:
    parent = [Step(index=0, kind=StepKind.PLAN, thought="delegate")]
    subagents = {
        "implementer": [Step(index=0, kind=StepKind.TOOL_CALL, tool_call=ToolCall(tool="edit"))],
        "tester": [Step(index=0, kind=StepKind.OBSERVATION, observation="tests pass")],
    }
    trace = RunTraceAssembler().assemble(uuid.uuid4(), parent, subagent_steps=subagents)
    assert trace.has_subagents is True
    assert trace.total_steps == 3
    # Sub-agent steps are tagged with their originating role.
    sub_roles = {
        s.metadata.get("subagent_role") for s in trace.steps if s.metadata.get("subagent_role")
    }
    assert sub_roles == {"implementer", "tester"}
    # The whole timeline is contiguously reindexed.
    assert [s.index for s in trace.steps] == [0, 1, 2]
