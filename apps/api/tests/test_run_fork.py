"""Integration tests for ``POST /agent/runs/{run_id}/fork`` (Time-Travel Runs).

Loads a persisted ``RunRecording`` cassette, replays it up to ``fork_index`` and
lets it diverge against a *different* model client (injected via the per-role
``model_client_factory`` seam, overridden here to a deterministic scripted
client — no live provider). Hermetic SQLite, mirrors ``test_run_replay.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

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
from forge_api.routers.agent import get_fork_model_factory
from forge_contracts import AgentObjective, ModelClient, UserRole
from forge_db.base import Base
from forge_db.models import RunRecording, Workspace

WS = uuid.UUID("00000000-0000-0000-0000-0000000000d1")
WS2 = uuid.UUID("00000000-0000-0000-0000-0000000000d2")
NEW_MODEL = "claude-sonnet-5"


def _objective(text: str = "edit main") -> AgentObjective:
    return AgentObjective(objective=text, allowed_actions=["read_repo"])


def _record() -> RunCassette:
    """Tape a run through the same wrappers/wiring the worker uses today.

    Two LLM calls (plan + observe/finish) and one tool dispatch, over an empty
    registry (mirrors ``build_agent_runner``'s recording path and the fork
    endpoint's default empty registry so tool schemas do not spuriously diverge).
    """
    cassette = RunCassette()
    model = RecordingModelClient(
        ScriptedModelClient(
            [
                tool_response("read_file", {"path": "app/main.py", "action": "read_repo"}),
                finish_response("original done", confidence=0.9),
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


class _CapturingFactory:
    """A fork model-client factory that records the model it was asked for and
    the scripted client it handed back (so a test can inspect post-fork calls).
    """

    def __init__(self, responses_factory: Callable[[], list]) -> None:
        self._responses_factory = responses_factory
        self.calls: list[tuple[str, ScriptedModelClient]] = []

    def __call__(self) -> Callable[[str], ModelClient]:
        def factory(model: str) -> ModelClient:
            client = ScriptedModelClient(self._responses_factory())
            self.calls.append((model, client))
            return client

        return factory


def _client(
    factory: sessionmaker[Session],
    principal: Principal,
    *,
    fork_factory: _CapturingFactory | None = None,
) -> TestClient:
    app: FastAPI = create_app()

    def _get_db() -> Iterator[Session]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_principal] = lambda: principal
    if fork_factory is not None:
        app.dependency_overrides[get_fork_model_factory] = fork_factory
    return TestClient(app)


def test_fork_replays_prefix_then_new_client_finishes_differently(factory) -> None:
    recording_id = _seed_recording(factory)
    fork_factory = _CapturingFactory(lambda: [finish_response("forked conclusion", confidence=0.9)])
    client = _client(factory, _principal(), fork_factory=fork_factory)

    resp = client.post(
        f"/agent/runs/{recording_id}/fork",
        json={
            "objective": _objective().model_dump(mode="json"),
            "fork_index": 1,
            "model": NEW_MODEL,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["run_recording_id"] == str(recording_id)
    assert body["fork_index"] == 1
    assert body["model"] == NEW_MODEL
    assert body["diverged"] is False
    assert body["divergence"] is None

    # The counterfactual run took the new client's conclusion, not the tape's.
    assert body["result"] is not None
    assert body["result"]["status"] == "succeeded"
    assert body["result"]["output"] == "forked conclusion"

    # The factory was asked for the new model, and exactly one completion (the
    # post-fork observe/finish turn) was delegated to that new client — the
    # plan turn (llm 0) came off the tape.
    assert len(fork_factory.calls) == 1
    model_asked, new_client = fork_factory.calls[0]
    assert model_asked == NEW_MODEL
    assert len(new_client.requests) == 1


def test_fork_index_zero_runs_fully_on_the_new_client(factory) -> None:
    recording_id = _seed_recording(factory)
    fork_factory = _CapturingFactory(
        lambda: [finish_response("forked from the start", confidence=0.9)]
    )
    client = _client(factory, _principal(), fork_factory=fork_factory)

    resp = client.post(
        f"/agent/runs/{recording_id}/fork",
        json={
            "objective": _objective().model_dump(mode="json"),
            "fork_index": 0,
            "model": NEW_MODEL,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["diverged"] is False
    assert body["result"]["output"] == "forked from the start"
    # fork_index=0 -> the plan turn already runs on the new client.
    _, new_client = fork_factory.calls[0]
    assert len(new_client.requests) == 1


def test_fork_prompt_override_is_applied_to_post_fork_requests(factory) -> None:
    recording_id = _seed_recording(factory)
    fork_factory = _CapturingFactory(
        lambda: [finish_response("forked with override", confidence=0.9)]
    )
    client = _client(factory, _principal(), fork_factory=fork_factory)

    resp = client.post(
        f"/agent/runs/{recording_id}/fork",
        json={
            "objective": _objective().model_dump(mode="json"),
            "fork_index": 1,
            "model": NEW_MODEL,
            "prompt_override": "ALWAYS PREFER THE SAFEST OPTION.",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["output"] == "forked with override"

    _, new_client = fork_factory.calls[0]
    assert new_client.requests, "new client should have received the post-fork request"
    assert "ALWAYS PREFER THE SAFEST OPTION." in new_client.requests[0].system


def test_fork_diverges_when_the_prefix_objective_differs(factory) -> None:
    recording_id = _seed_recording(factory)
    fork_factory = _CapturingFactory(lambda: [finish_response("unreached", confidence=0.9)])
    client = _client(factory, _principal(), fork_factory=fork_factory)

    resp = client.post(
        f"/agent/runs/{recording_id}/fork",
        json={
            "objective": _objective("a completely different objective").model_dump(mode="json"),
            "fork_index": 1,
            "model": NEW_MODEL,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # The pre-fork plan turn (llm 0) no longer matches the tape: the fork aborts.
    assert body["diverged"] is True
    assert body["result"] is None
    assert body["divergence"]["boundary"] == "llm"
    assert body["divergence"]["index"] == 0
    # The new client was never reached (divergence happened in the replayed prefix).
    _, new_client = fork_factory.calls[0]
    assert new_client.requests == []


def test_fork_unknown_recording_is_404(factory) -> None:
    client = _client(factory, _principal())
    resp = client.post(
        f"/agent/runs/{uuid.uuid4()}/fork",
        json={"objective": _objective().model_dump(mode="json"), "model": NEW_MODEL},
    )
    assert resp.status_code == 404


def test_fork_cross_workspace_recording_is_404(factory) -> None:
    recording_id = _seed_recording(factory, workspace_id=WS)
    other_workspace_client = _client(factory, _principal(workspace_id=WS2))

    resp = other_workspace_client.post(
        f"/agent/runs/{recording_id}/fork",
        json={"objective": _objective().model_dump(mode="json"), "model": NEW_MODEL},
    )
    assert resp.status_code == 404
