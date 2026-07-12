"""Integration tests for ``POST /agent/runs/{run_id}/replay`` (Time-Travel Runs).

Loads a persisted ``RunRecording`` cassette and replays it by substitution
through a fresh ``AgentRunner`` — real handlers, hermetic SQLite (mirrors
``test_audit_api.py``'s convention), no live model or tool ever touched.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

from forge_agent.replay import RecordingModelClient, RecordingToolRegistry, RunCassette
from forge_agent.runtime import AgentRunner
from forge_agent.testing import ScriptedModelClient, finish_response, tool_response
from forge_agent.tools import ToolRegistry
from forge_api.db import get_db
from forge_api.deps import Principal, get_current_principal
from forge_api.main import create_app
from forge_contracts import AgentObjective, UserRole
from forge_db.base import Base
from forge_db.models import RunRecording, Workspace

WS = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
WS2 = uuid.UUID("00000000-0000-0000-0000-0000000000c2")


def _objective(text: str = "edit main") -> AgentObjective:
    return AgentObjective(objective=text, allowed_actions=["read_repo"])


def _record() -> RunCassette:
    """Tape a run through the *same* wrappers/wiring the worker uses today.

    ``build_agent_runner`` (``apps/worker/forge_worker/agent_runner.py``) wraps
    an *empty* ``ToolRegistry`` when recording (no real tools wired into the
    worker's recording path yet) — mirrored here so the replay endpoint's
    default (also an empty registry — see ``replay_service.replay_recording``)
    exposes matching tool schemas and does not spuriously diverge on them.
    """
    cassette = RunCassette()
    model = RecordingModelClient(
        ScriptedModelClient(
            [
                tool_response("read_file", {"path": "app/main.py", "action": "read_repo"}),
                finish_response("done", confidence=0.9),
            ]
        ),
        cassette,
    )
    tools = RecordingToolRegistry(ToolRegistry(), cassette)
    AgentRunner(model=model, tools=tools).run(_objective())
    return cassette


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sf: sessionmaker[Session] = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with sf() as s:
        s.add(Workspace(id=WS, name="Acme", slug="acme"))
        s.add(Workspace(id=WS2, name="Rival", slug="rival"))
        s.commit()
    yield sf
    engine.dispose()


def _seed_recording(factory: sessionmaker[Session], *, workspace_id: uuid.UUID = WS) -> uuid.UUID:
    cassette = _record()
    with factory() as s:
        row = RunRecording(
            workspace_id=workspace_id,
            cassette=cassette.to_dict(),
            model=cassette.llm_calls[-1].model,
            content_hash="a" * 64,
        )
        s.add(row)
        s.commit()
        recording_id = row.id
    return recording_id


def _principal(workspace_id: uuid.UUID = WS) -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        workspace_id=workspace_id,
        role=UserRole.MEMBER,
        email="member@acme.test",
        auth_method="test",
        scopes=["*"],
    )


def _client(factory: sessionmaker[Session], principal: Principal) -> TestClient:
    app: FastAPI = create_app()

    def _get_db() -> Iterator[Session]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_principal] = lambda: principal
    return TestClient(app)


def test_replay_with_the_same_objective_does_not_diverge(factory) -> None:
    recording_id = _seed_recording(factory)
    client = _client(factory, _principal())

    resp = client.post(
        f"/agent/runs/{recording_id}/replay",
        json={"objective": _objective().model_dump(mode="json")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["run_recording_id"] == str(recording_id)
    assert body["diverged"] is False
    assert body["divergence"] is None
    assert body["result"] is not None
    assert body["result"]["status"] == "succeeded"

    # Two LLM calls (plan + observe/finish) + one tool call, all matched.
    assert len(body["steps"]) == 3
    assert all(step["matched"] for step in body["steps"])
    assert {(s["boundary"], s["index"]) for s in body["steps"]} == {
        ("llm", 0),
        ("llm", 1),
        ("tool", 0),
    }
    tool_step = next(s for s in body["steps"] if s["boundary"] == "tool")
    assert tool_step["name"] == "read_file"
    assert tool_step["recorded_digest"] == tool_step["replay_digest"]


def test_replay_diverges_when_the_objective_differs(factory) -> None:
    recording_id = _seed_recording(factory)
    client = _client(factory, _principal())

    resp = client.post(
        f"/agent/runs/{recording_id}/replay",
        json={"objective": _objective("a completely different objective").model_dump(mode="json")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["diverged"] is True
    assert body["result"] is None
    assert body["divergence"] == {
        "boundary": "llm",
        "index": 0,
        "name": None,
        "expected": body["divergence"]["expected"],
        "actual": body["divergence"]["actual"],
    }
    assert body["divergence"]["expected"] != body["divergence"]["actual"]

    # The very first LLM call already diverged: nothing downstream was attempted.
    steps_by_key = {(s["boundary"], s["index"]): s for s in body["steps"]}
    first_llm = steps_by_key[("llm", 0)]
    assert first_llm["matched"] is False
    assert first_llm["replay_digest"] == body["divergence"]["actual"]
    assert steps_by_key[("llm", 1)]["matched"] is False
    assert steps_by_key[("llm", 1)]["replay_digest"] is None
    assert steps_by_key[("tool", 0)]["matched"] is False
    assert steps_by_key[("tool", 0)]["replay_digest"] is None


def test_replay_unknown_recording_is_404(factory) -> None:
    client = _client(factory, _principal())
    resp = client.post(
        f"/agent/runs/{uuid.uuid4()}/replay",
        json={"objective": _objective().model_dump(mode="json")},
    )
    assert resp.status_code == 404


def test_replay_cross_workspace_recording_is_404(factory) -> None:
    recording_id = _seed_recording(factory, workspace_id=WS)
    other_workspace_client = _client(factory, _principal(workspace_id=WS2))

    resp = other_workspace_client.post(
        f"/agent/runs/{recording_id}/replay",
        json={"objective": _objective().model_dump(mode="json")},
    )
    assert resp.status_code == 404
