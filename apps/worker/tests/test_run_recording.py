"""Tests for the worker-side Time-Travel Runs recorder sink (cassette-persistence).

Hermetic: no live model provider, no Celery broker, no live Postgres — the DB
assertions run against an in-memory SQLite engine (mirrors ``packages/db``'s own
unit-test convention).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from forge_agent import AgentRunner
from forge_agent.replay import RecordingModelClient, RecordingToolRegistry, RunCassette
from forge_agent.testing import ScriptedModelClient, finish_response, tool_response
from forge_agent.tools import ToolRegistry, ToolResult
from forge_contracts import AgentObjective, ModelRequest, ModelResponse
from forge_db.base import Base
from forge_db.models import AgentRun, Project, RunRecording, Task, WorkflowRun, Workspace
from forge_worker.agent_runner import (
    build_agent_runner,
    persist_run_recording,
    run_agent_task,
    run_objective,
)


class _FakeArtifactStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, key: str, data: bytes, *, content_type: str = "text/plain") -> str:
        self.objects[key] = data
        return f"artifact://{key}"


@pytest.fixture
def sqlite_session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            yield session
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _seed(session: Session) -> uuid.UUID:
    ws = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    session.add(ws)
    session.flush()
    return ws.id


# --------------------------------------------------------------------------- #
# build_agent_runner — FORGE_RECORD_RUNS gating                               #
# --------------------------------------------------------------------------- #


def test_recording_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORGE_RECORD_RUNS", raising=False)
    injected = ScriptedModelClient(responses=[finish_response("done", confidence=0.9)])
    runner = build_agent_runner(model_client=injected)
    assert runner.cassette is None
    # The model client is used verbatim (not wrapped) when recording is off.
    assert runner._model is injected


def test_recording_enabled_wraps_boundaries_and_captures_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FORGE_RECORD_RUNS", "1")
    injected = ScriptedModelClient(responses=[finish_response("done", confidence=0.9)])
    runner = build_agent_runner(model_client=injected)

    assert isinstance(runner.cassette, RunCassette)
    assert isinstance(runner._model, RecordingModelClient)
    assert isinstance(runner._tools, RecordingToolRegistry)

    result = run_objective(runner, AgentObjective(objective="do the thing"))
    assert result.steps
    # The plan call the runtime made was recorded onto the cassette.
    assert len(runner.cassette.llm_calls) == 1
    assert runner.cassette.llm_calls[0].response is injected._responses[0]


def test_recording_env_snapshot_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGE_RECORD_RUNS", "1")
    monkeypatch.setenv("SOME_API_KEY", "sk-supersecrettoken1234567890")
    runner = build_agent_runner(model_client=ScriptedModelClient(responses=[]))
    assert runner.cassette is not None
    assert "sk-supersecrettoken1234567890" not in runner.cassette.env["SOME_API_KEY"]


# --------------------------------------------------------------------------- #
# persist_run_recording — the recorder sink                                   #
# --------------------------------------------------------------------------- #


def _wire_recording_run() -> RunCassette:
    inner_registry = ToolRegistry()
    inner_registry.add(
        "read_file",
        lambda _a: ToolResult(ok=True, output="a" * 20),
        action="read_repo",
    )
    cassette = RunCassette()
    model = RecordingModelClient(
        ScriptedModelClient(
            [
                tool_response("read_file", {"path": "x", "action": "read_repo"}),
                finish_response("done", confidence=0.9),
            ]
        ),
        cassette,
    )
    tools = RecordingToolRegistry(inner_registry, cassette)
    AgentRunner(model=model, tools=tools).run(
        AgentObjective(objective="edit main", allowed_actions=["read_repo"])
    )
    return cassette


def test_persist_run_recording_inserts_a_row(sqlite_session: Session) -> None:
    ws_id = _seed(sqlite_session)
    cassette = _wire_recording_run()

    row = persist_run_recording(sqlite_session, cassette, workspace_id=ws_id)
    sqlite_session.commit()

    assert isinstance(row, RunRecording)
    assert row.workspace_id == ws_id
    assert row.agent_run_id is None
    assert row.workflow_run_id is None
    assert row.model is not None
    assert len(row.content_hash) == 64

    loaded = sqlite_session.get(RunRecording, row.id)
    assert loaded is not None
    assert len(loaded.cassette["llm_calls"]) == 2
    assert len(loaded.cassette["tool_calls"]) == 1


def test_persist_run_recording_links_agent_and_workflow_run(sqlite_session: Session) -> None:
    ws_id = _seed(sqlite_session)
    project = Project(workspace_id=ws_id, name="Core", key="COR")
    sqlite_session.add(project)
    sqlite_session.flush()
    task = Task(workspace_id=ws_id, project_id=project.id, key="T-1", title="t")
    sqlite_session.add(task)
    sqlite_session.flush()
    wf_run = WorkflowRun(workspace_id=ws_id, task_id=task.id)
    sqlite_session.add(wf_run)
    sqlite_session.flush()
    agent_run = AgentRun(workspace_id=ws_id, workflow_run_id=wf_run.id, task_id=task.id)
    sqlite_session.add(agent_run)
    sqlite_session.flush()

    cassette = _wire_recording_run()
    row = persist_run_recording(
        sqlite_session,
        cassette,
        workspace_id=ws_id,
        agent_run_id=agent_run.id,
        workflow_run_id=wf_run.id,
    )
    sqlite_session.commit()
    assert row.agent_run_id == agent_run.id
    assert row.workflow_run_id == wf_run.id


def test_persist_run_recording_redacts_secrets(sqlite_session: Session) -> None:
    ws_id = _seed(sqlite_session)
    cassette = RunCassette()
    cassette.record_llm(
        ModelRequest(model="m", system="sys"),
        ModelResponse(content="here is API_KEY=sk-live-supersecrettoken1234567890", model="m"),
    )
    row = persist_run_recording(sqlite_session, cassette, workspace_id=ws_id)
    sqlite_session.commit()
    assert "sk-live-supersecrettoken1234567890" not in str(row.cassette)


def test_persist_run_recording_offloads_oversized_tool_output(sqlite_session: Session) -> None:
    ws_id = _seed(sqlite_session)
    oversized = "a" * 300_000  # exceeds the 262144-byte default cap
    inner_registry = ToolRegistry()
    inner_registry.add(
        "read_file",
        lambda _a: ToolResult(ok=True, output=oversized),
        action="read_repo",
    )
    cassette = RunCassette()
    tools = RecordingToolRegistry(inner_registry, cassette)
    dispatched = tools.dispatch("read_file", {"path": "x"})
    # The live caller (the agent loop) still gets the full, uncapped output —
    # only the *persisted* copy is capped/offloaded.
    assert dispatched.output == oversized

    store = _FakeArtifactStore()
    row = persist_run_recording(sqlite_session, cassette, workspace_id=ws_id, artifact_store=store)
    sqlite_session.commit()

    persisted_result = row.cassette["tool_calls"][0]["result"]
    assert len(persisted_result["output"]) < len(oversized)
    assert persisted_result["output_artifact_ref"] is not None
    assert len(store.objects) == 1
    assert next(iter(store.objects.values())) == oversized.encode("utf-8")


def test_run_recording_is_append_only_by_convention(sqlite_session: Session) -> None:
    """SQLite has no immutability trigger (Postgres-only); the model exposes no
    update helper here either — the row is inserted once via ``persist_run_recording``
    and never mutated again in this codepath (see ``test_run_recording_models.py``
    for the Postgres-enforced guarantee)."""
    ws_id = _seed(sqlite_session)
    cassette = _wire_recording_run()
    row = persist_run_recording(sqlite_session, cassette, workspace_id=ws_id)
    sqlite_session.commit()
    original_hash = row.content_hash
    reloaded = sqlite_session.get(RunRecording, row.id)
    assert reloaded is not None
    assert reloaded.content_hash == original_hash


# --------------------------------------------------------------------------- #
# run_agent_task — best-effort wiring (no workspace context -> no-op)          #
# --------------------------------------------------------------------------- #


def test_run_agent_task_skips_persist_without_workspace_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FORGE_RECORD_RUNS", "1")
    # No context.workspace_id supplied -> the recorder sink is a documented
    # no-op (never raises, never fabricates a workspace).
    result = run_agent_task({"objective": "do the thing"})
    assert isinstance(result, dict)
    assert "status" in result


def test_run_agent_task_still_works_when_recording_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FORGE_RECORD_RUNS", raising=False)
    result = run_agent_task({"objective": "do the thing 2"})
    assert isinstance(result, dict)
    assert "status" in result
