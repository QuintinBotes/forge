"""Tests for the worker agent-runner task (plan Task 1.9 — single-agent loop).

Hermetic: an offline scripted model client drives the runtime; no network, no
live model provider, no Celery broker. The supervised multi-agent dispatch tests
(Task 17) use an in-memory SQLite engine for the ``sub_agent_run`` persistence
assertions (mirrors ``test_run_recording.py``).
"""

from __future__ import annotations

import importlib.util
import logging
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

from forge_agent import AgentRunner
from forge_agent.testing import ScriptedModelClient, finish_response
from forge_contracts import AgentObjective
from forge_contracts.enums import RunStatus
from forge_db.base import Base
from forge_db.models import AgentRun, RunRecording, SubAgentRun, Workspace
from forge_db.models.enums import RunStatus as DbRunStatus
from forge_worker import agent_runner as agent_runner_module
from forge_worker import multi_agent
from forge_worker.agent_runner import build_agent_runner, run_agent_task, run_objective


def test_run_objective_completes_offline() -> None:
    runner = AgentRunner(
        ScriptedModelClient(
            responses=[finish_response("done", confidence=0.95)],
            default=finish_response("done", confidence=0.95),
        )
    )
    result = run_objective(runner, AgentObjective(objective="Investigate the bug"))
    assert result.status == RunStatus.SUCCEEDED
    assert result.steps


def test_build_agent_runner_is_offline_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORGE_MODEL_PROVIDER", raising=False)
    runner = build_agent_runner()
    result = run_objective(runner, AgentObjective(objective="Do the thing"))
    # Default scripted model finishes cleanly without any live provider call.
    assert result.status in {RunStatus.SUCCEEDED, RunStatus.ESCALATED}


def test_build_agent_runner_no_creds_uses_scripted(monkeypatch: pytest.MonkeyPatch) -> None:
    """HARD-02 AC8: no provider creds -> the offline scripted client."""
    monkeypatch.delenv("FORGE_MODEL_PROVIDER", raising=False)
    runner = build_agent_runner()
    assert isinstance(runner._model, ScriptedModelClient)


def test_build_agent_runner_warns_on_scripted_fallback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No provider configured -> a single loud WARNING that output is canned.

    A self-hoster who never set ``FORGE_MODEL_PROVIDER`` gets ``ScriptedModelClient``
    (offline, deterministic) instead of a real agent run; the fallback must not be
    silent. The warning fires once per resolve (once per run), not per-call spam.
    """
    monkeypatch.delenv("FORGE_MODEL_PROVIDER", raising=False)
    with caplog.at_level(logging.WARNING, logger="forge.agent_runner"):
        build_agent_runner()
    fallback_warnings = [
        r for r in caplog.records if r.name == "forge.agent_runner" and r.levelno == logging.WARNING
    ]
    assert len(fallback_warnings) == 1, "expected exactly one scripted-fallback WARNING"
    message = fallback_warnings[0].getMessage()
    assert "ScriptedModelClient" in message
    assert "FORGE_MODEL_PROVIDER" in message


def test_build_agent_runner_uses_injected_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """HARD-02 AC8: an injected real ``ModelClient`` is used verbatim."""
    monkeypatch.delenv("FORGE_MODEL_PROVIDER", raising=False)
    injected = ScriptedModelClient(responses=[finish_response("injected", confidence=0.9)])
    runner = build_agent_runner(model_client=injected)
    assert runner._model is injected


@pytest.mark.skipif(
    importlib.util.find_spec("anthropic") is not None,
    reason="providers extra installed — SDK-missing fallback not exercised on this lane",
)
def test_build_agent_runner_falls_back_when_sdk_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HARD-02: creds present but the provider SDK absent -> scripted, never crash."""
    monkeypatch.setenv("FORGE_MODEL_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    runner = build_agent_runner()
    assert isinstance(runner._model, ScriptedModelClient)


def test_celery_task_is_registered() -> None:
    from forge_worker.celery_app import celery_app

    assert "forge.agent.run" in celery_app.tasks
    assert run_agent_task.name == "forge.agent.run"


def test_run_agent_task_executes_and_serializes() -> None:
    # Calling the Celery task object runs the task body synchronously (no broker).
    result = run_agent_task({"objective": "do the thing"})
    assert isinstance(result, dict)
    assert result["status"] in {RunStatus.SUCCEEDED.value, RunStatus.ESCALATED.value}
    assert "steps" in result
    # The result is JSON-serialisable (mode="json"): no raw UUID/enum objects.
    import json

    json.dumps(result)


# --------------------------------------------------------------------------- #
# Supervised multi-agent dispatch (Task 17 — F27 wiring)                       #
# --------------------------------------------------------------------------- #


@pytest.fixture
def sqlite_engine() -> Iterator[Engine]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _seed_workspace(engine: Engine) -> uuid.UUID:
    with Session(engine) as session:
        ws = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
        session.add(ws)
        session.commit()
        return ws.id


def _supervised_payload(workspace_id: uuid.UUID, parent_id: uuid.UUID) -> dict[str, Any]:
    """A supervised-mode objective payload as the Celery task receives it (JSON)."""
    return {
        "task_id": str(uuid.uuid4()),
        "key": "TASK-17",
        "objective": "Implement the feature under supervision",
        "execution_mode": "supervised_multi_agent",
        "subagent_policy": {"allowed": True, "max_parallel": 2},
        "context": {
            "workspace_id": str(workspace_id),
            "parent_agent_run_id": str(parent_id),
            "review_required": True,
            "subagent_rules": {
                "allow_subagents": True,
                "allowed_roles": ["implementer", "reviewer"],
                "max_parallel": 2,
            },
        },
    }


def _use_sqlite(monkeypatch: pytest.MonkeyPatch, engine: Engine) -> None:
    """Point the adapter's DB seam at the hermetic SQLite engine."""
    monkeypatch.setattr(multi_agent, "create_session_factory", lambda: lambda: Session(engine))


def test_supervised_mode_dispatches_coordinator_and_persists_rows(
    monkeypatch: pytest.MonkeyPatch, sqlite_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """The supervised branch drives forge_coordinator end-to-end, offline.

    Asserts the full Task-17 contract: the run completes through the Celery task
    with the single-path observable contract (a JSON-serialisable AgentRunResult
    dict), coordinator persistence rows exist (``sub_agent_run`` per specialist +
    the supervisor's own ``agent_run`` row), and model-client construction reused
    ``_resolve_model_client`` (the Task-1 scripted-fallback WARNING fires).
    """
    monkeypatch.delenv("FORGE_MODEL_PROVIDER", raising=False)
    monkeypatch.delenv("FORGE_RECORD_RUNS", raising=False)
    monkeypatch.setenv("MULTI_AGENT_ENABLED", "1")
    _use_sqlite(monkeypatch, sqlite_engine)

    ws_id = _seed_workspace(sqlite_engine)
    parent_id = uuid.uuid4()

    with caplog.at_level(logging.WARNING, logger="forge.agent_runner"):
        result = run_agent_task(_supervised_payload(ws_id, parent_id))

    # Same observable contract as the single path: a JSON-safe result dict.
    import json

    json.dumps(result)
    assert result["status"] == RunStatus.SUCCEEDED.value
    assert result["needs_human"] is False
    assert result["artifacts"]["pattern"] == "maker_checker"
    assert result["artifacts"]["is_supervisor"] is True
    assert result["run_id"] == str(parent_id)

    # Coordinator persistence rows: one sub_agent_run per spawned specialist.
    with Session(sqlite_engine) as session:
        rows = session.execute(select(SubAgentRun).order_by(SubAgentRun.ordinal)).scalars().all()
        assert [r.role for r in rows] == ["implementer", "reviewer"]
        assert all(r.parent_agent_run_id == parent_id for r in rows)
        assert all(r.workspace_id == ws_id for r in rows)
        assert all(r.status is DbRunStatus.SUCCEEDED for r in rows)
        # The supervisor's own agent_run row exists and was finalized.
        parent = session.get(AgentRun, parent_id)
        assert parent is not None
        assert parent.is_supervisor is True
        assert parent.status is DbRunStatus.SUCCEEDED
        assert parent.pattern == "maker_checker"
        assert parent.completed_at is not None

    # Model clients came through _resolve_model_client: the Task-1 loud fallback.
    fallback = [
        r
        for r in caplog.records
        if r.name == "forge.agent_runner"
        and r.levelno == logging.WARNING
        and "ScriptedModelClient" in r.getMessage()
    ]
    assert len(fallback) == 1, "expected exactly one scripted-fallback WARNING per run"


@pytest.mark.parametrize(
    "payload",
    [
        {"objective": "do the thing"},
        {"objective": "do the thing", "execution_mode": "single_agent"},
    ],
)
def test_default_mode_builds_exactly_one_agent_runner(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]
) -> None:
    """Regression: non-supervised modes still build exactly one AgentRunner."""
    monkeypatch.delenv("FORGE_MODEL_PROVIDER", raising=False)
    calls: list[int] = []
    real_build = agent_runner_module.build_agent_runner

    def _counting_build(**kwargs: Any) -> AgentRunner:
        calls.append(1)
        return real_build(**kwargs)

    monkeypatch.setattr(agent_runner_module, "build_agent_runner", _counting_build)

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the supervised path must not run for non-supervised modes")

    monkeypatch.setattr(multi_agent, "run_supervised_objective", _boom)

    result = run_agent_task(payload)
    assert len(calls) == 1
    assert result["status"] in {RunStatus.SUCCEEDED.value, RunStatus.ESCALATED.value}


def test_supervised_mode_single_runner_never_built(
    monkeypatch: pytest.MonkeyPatch, sqlite_engine: Engine
) -> None:
    """The supervised branch never builds the single-agent runner."""
    monkeypatch.delenv("FORGE_MODEL_PROVIDER", raising=False)
    monkeypatch.setenv("MULTI_AGENT_ENABLED", "1")
    _use_sqlite(monkeypatch, sqlite_engine)

    def _boom(**kwargs: Any) -> Any:
        raise AssertionError("build_agent_runner must not be called in supervised mode")

    monkeypatch.setattr(agent_runner_module, "build_agent_runner", _boom)
    ws_id = _seed_workspace(sqlite_engine)
    result = run_agent_task(_supervised_payload(ws_id, uuid.uuid4()))
    assert result["status"] == RunStatus.SUCCEEDED.value


def test_supervised_mode_disabled_gate_escalates_without_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MULTI_AGENT_ENABLED unset -> the coordinator's own disabled gate escalates.

    Also covers the DB-unavailable degrade: with no reachable database the
    adapter falls back to the in-memory sink instead of failing the run.
    """
    monkeypatch.delenv("FORGE_MODEL_PROVIDER", raising=False)
    monkeypatch.delenv("MULTI_AGENT_ENABLED", raising=False)

    def _no_db() -> Any:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(multi_agent, "create_session_factory", _no_db)

    result = run_agent_task(_supervised_payload(uuid.uuid4(), uuid.uuid4()))
    assert result["status"] == RunStatus.ESCALATED.value
    assert result["needs_human"] is True
    assert result["artifacts"]["needs_human_reason"] == "multi_agent_disabled"


def test_supervised_mode_recording_is_honest_noop(
    monkeypatch: pytest.MonkeyPatch, sqlite_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """FORGE_RECORD_RUNS=1 on the supervised path: loud no-op, no RunRecording rows."""
    monkeypatch.delenv("FORGE_MODEL_PROVIDER", raising=False)
    monkeypatch.setenv("FORGE_RECORD_RUNS", "1")
    monkeypatch.setenv("MULTI_AGENT_ENABLED", "1")
    _use_sqlite(monkeypatch, sqlite_engine)

    ws_id = _seed_workspace(sqlite_engine)
    with caplog.at_level(logging.WARNING, logger="forge.multi_agent"):
        result = run_agent_task(_supervised_payload(ws_id, uuid.uuid4()))

    assert result["status"] == RunStatus.SUCCEEDED.value
    with Session(sqlite_engine) as session:
        assert session.execute(select(RunRecording)).scalars().all() == []
    assert any(
        "FORGE_RECORD_RUNS" in r.getMessage() and r.levelno == logging.WARNING
        for r in caplog.records
        if r.name == "forge.multi_agent"
    )
