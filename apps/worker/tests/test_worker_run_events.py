"""The worker's run-event sink (ADR-0013, spec L seam 3)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import forge_worker.agent_runner as agent_runner_module
from forge_contracts import (
    ENFORCEMENT,
    AgentRunResult,
    Decision,
    DecisionEffect,
    RunEventType,
    RunStatus,
    Step,
    StepKind,
    ToolCall,
)
from forge_db.base import Base
from forge_db.models import AgentRun, AgentRunEvent, Workspace
from forge_worker.agent_runner import run_agent_task
from forge_worker.reliability import InMemoryDedupBackend, set_dedup_backend
from forge_worker.run_events import MAX_TEXT, RunEventSink, terminal_drafts


@pytest.fixture
def factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def fresh_dedup():
    """Isolate the process-wide task dedup backend (mirrors test_reliability)."""
    set_dedup_backend(InMemoryDedupBackend())
    yield
    set_dedup_backend(None)


def _run(factory: sessionmaker[Session]) -> tuple[uuid.UUID, uuid.UUID]:
    with factory() as session:
        workspace = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:6]}")
        session.add(workspace)
        session.flush()
        run = AgentRun(workspace_id=workspace.id)
        session.add(run)
        session.commit()
        return workspace.id, run.id


def _events(factory: sessionmaker[Session], run_id: uuid.UUID) -> list[AgentRunEvent]:
    with factory() as session:
        return list(
            session.scalars(
                select(AgentRunEvent)
                .where(AgentRunEvent.run_id == run_id)
                .order_by(AgentRunEvent.seq)
            )
        )


def test_a_denied_call_is_stored_as_request_then_denial(factory) -> None:
    workspace_id, run_id = _run(factory)
    sink = RunEventSink(run_id, workspace_id, session_factory=factory, redact=lambda s: s)
    sink.on_step(
        Step(
            kind=StepKind.DECISION,
            tool_call=ToolCall(tool="run_command", arguments={"cmd": "curl evil"}),
            decision=Decision(effect=DecisionEffect.DENY, matched_rule="no-network"),
        )
    )
    rows = _events(factory, run_id)
    assert [(r.seq, r.type, r.enforcement) for r in rows] == [
        (1, "tool.requested", "observed"),
        (2, "permission.denied", "gated"),
    ]


def test_payload_text_is_redacted_and_truncated(factory) -> None:
    workspace_id, run_id = _run(factory)
    sink = RunEventSink(
        run_id,
        workspace_id,
        session_factory=factory,
        redact=lambda s: s.replace("ghp_secret", "[REDACTED]"),
    )
    sink.on_step(
        Step(
            kind=StepKind.TOOL_CALL,
            tool_call=ToolCall(tool="run_tests"),
            output="token ghp_secret " + "x" * 5000,
        )
    )
    (row,) = _events(factory, run_id)
    output = row.payload["output"]
    assert "ghp_secret" not in output
    assert "[REDACTED]" in output
    assert len(output) == MAX_TEXT


def test_numbering_resumes_after_existing_events(factory) -> None:
    workspace_id, run_id = _run(factory)
    first = RunEventSink(run_id, workspace_id, session_factory=factory, redact=str)
    first.on_step(Step(kind=StepKind.PLAN, thought="plan"))
    second = RunEventSink(run_id, workspace_id, session_factory=factory, redact=str)
    second.on_step(Step(kind=StepKind.OUTPUT, output="done"))
    assert [r.seq for r in _events(factory, run_id)] == [1, 2]


def test_a_failing_store_never_raises() -> None:
    def broken() -> Session:
        raise RuntimeError("database down")

    sink = RunEventSink(uuid.uuid4(), uuid.uuid4(), session_factory=broken, redact=str)
    sink.on_step(Step(kind=StepKind.PLAN, thought="still runs"))


def test_terminal_drafts_report_the_pr_cost_and_outcome() -> None:
    result = AgentRunResult(
        status=RunStatus.SUCCEEDED,
        summary="Added search",
        artifacts={
            "pull_request": {"status": "opened", "number": 7, "url": "https://x/pull/7"},
            "model_usage": {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.01},
        },
    )
    assert [d.type for d in terminal_drafts(result)] == [
        RunEventType.ARTIFACT_CREATED,
        RunEventType.COST_UPDATED,
        RunEventType.THREAD_COMPLETED,
    ]
    failed = terminal_drafts(AgentRunResult(status=RunStatus.FAILED, error="budget_exhausted"))
    assert [d.type for d in failed] == [RunEventType.THREAD_FAILED]
    assert failed[0].payload["error"] == "budget_exhausted"


def test_a_real_run_streams_from_started_to_a_terminal_event(
    factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id, run_id = _run(factory)
    monkeypatch.setattr(agent_runner_module, "create_session_factory", lambda: factory)
    run_agent_task(
        {
            "objective": "do the thing",
            "context": {"agent_run_id": str(run_id), "workspace_id": str(workspace_id)},
        }
    )
    rows = _events(factory, run_id)
    assert rows[0].type == RunEventType.THREAD_STARTED.value
    assert rows[-1].type in {"thread.completed", "thread.failed"}
    assert [r.seq for r in rows] == list(range(1, len(rows) + 1))
    assert all(r.enforcement == ENFORCEMENT[RunEventType(r.type)] for r in rows)
    with factory() as session:
        run = session.get(AgentRun, run_id)
        assert run is not None and run.completed_at is not None


def test_soft_time_limit_records_terminal_escalated(factory, monkeypatch, fresh_dedup) -> None:
    """The soft-limit path closes the run out instead of leaving it PENDING."""
    from celery.exceptions import SoftTimeLimitExceeded

    workspace_id, run_id = _run(factory)
    monkeypatch.setattr(agent_runner_module, "create_session_factory", lambda: factory)

    def _raise(*_args, **_kwargs):
        raise SoftTimeLimitExceeded

    monkeypatch.setattr(agent_runner_module, "run_objective", _raise)
    out = run_agent_task(
        {
            "objective": "runaway",
            "context": {"agent_run_id": str(run_id), "workspace_id": str(workspace_id)},
        },
        idempotency_key="soft-limit-run",
    )
    assert out["escalated"] is True
    assert out["reason"] == "soft_time_limit_exceeded"
    rows = _events(factory, run_id)
    assert rows[-1].type == RunEventType.THREAD_FAILED.value
    assert rows[-1].payload["status"] == "escalated"
    with factory() as session:
        run = session.get(AgentRun, run_id)
        assert run is not None
        assert run.status.value == "escalated"
        assert run.output["error"] == "soft_time_limit_exceeded"
        assert run.completed_at is not None


def test_unhandled_exception_records_failed_then_reraises(
    factory, monkeypatch, fresh_dedup
) -> None:
    workspace_id, run_id = _run(factory)
    monkeypatch.setattr(agent_runner_module, "create_session_factory", lambda: factory)

    def _raise(*_args, **_kwargs):
        raise RuntimeError("upstream 503")

    monkeypatch.setattr(agent_runner_module, "run_objective", _raise)
    with pytest.raises(RuntimeError, match="upstream 503"):
        run_agent_task(
            {
                "objective": "doomed",
                "context": {"agent_run_id": str(run_id), "workspace_id": str(workspace_id)},
            },
            idempotency_key="unhandled-run",
        )
    rows = _events(factory, run_id)
    assert rows[-1].type == RunEventType.THREAD_FAILED.value
    with factory() as session:
        run = session.get(AgentRun, run_id)
        assert run is not None
        assert run.status.value == "failed"
        assert run.output["error"] == "RuntimeError"
        assert run.completed_at is not None
