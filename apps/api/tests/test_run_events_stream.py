"""``GET /agent/runs/{id}/events`` as Server-Sent Events (ADR-0013, spec L seam 3)."""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from forge_api.deps import Principal, get_current_principal
from forge_api.main import create_app
from forge_api.routers import run_events as run_events_router
from forge_contracts import UserRole
from forge_db.base import Base
from forge_db.models import AgentRun, AgentRunEvent, Workspace
from forge_db.models.enums import RunStatus

WS = uuid.UUID("00000000-0000-0000-0000-0000000000e1")
OTHER = uuid.UUID("00000000-0000-0000-0000-0000000000e2")


@pytest.fixture
def factory(tmp_path) -> Iterator[sessionmaker[Session]]:
    # A file database with a connection per thread: the live-follow test writes
    # from one thread while the stream reads from another, and a single shared
    # in-memory connection lets the reader's rollback undo the writer's insert.
    engine = create_engine(
        f"sqlite:///{tmp_path / 'events.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with sf() as session:
        session.add_all(
            [Workspace(id=WS, name="A", slug="a"), Workspace(id=OTHER, name="B", slug="b")]
        )
        session.commit()
    yield sf
    engine.dispose()


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_events_router, "POLL_SECONDS", 0.02)
    monkeypatch.setattr(run_events_router, "MAX_STREAM_SECONDS", 5.0)


def _client(factory: sessionmaker[Session], ws: uuid.UUID = WS) -> TestClient:
    app = create_app()
    principal = Principal(
        user_id=uuid.uuid4(),
        workspace_id=ws,
        role=UserRole.VIEWER,
        auth_method="test",
        scopes=["*"],
    )
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[run_events_router.get_run_event_session_factory] = lambda: factory
    return TestClient(app)


def _run(factory: sessionmaker[Session], status: RunStatus = RunStatus.RUNNING) -> uuid.UUID:
    with factory() as session:
        run = AgentRun(workspace_id=WS, status=status)
        session.add(run)
        session.commit()
        return run.id


def _event(factory, run_id, seq, type_, enforcement, summary="s") -> None:
    with factory() as session:
        session.add(
            AgentRunEvent(
                workspace_id=WS,
                run_id=run_id,
                seq=seq,
                type=type_,
                enforcement=enforcement,
                summary=summary,
                payload={"n": seq},
                ts=datetime.now(UTC),
            )
        )
        session.commit()


def _messages(text: str) -> list[dict]:
    out = []
    for block in text.strip().split("\n\n"):
        fields = dict(
            line.split(": ", 1) for line in block.splitlines() if line and not line.startswith(":")
        )
        if fields:
            out.append(
                {"id": int(fields["id"]), "event": fields["event"], **json.loads(fields["data"])}
            )
    return out


def _seed_finished_run(factory) -> uuid.UUID:
    run_id = _run(factory)
    _event(factory, run_id, 1, "thread.started", "gated")
    _event(factory, run_id, 2, "tool.requested", "observed")
    _event(factory, run_id, 3, "permission.denied", "gated")
    _event(factory, run_id, 4, "thread.failed", "observed")
    return run_id


def test_replays_every_event_in_order_with_its_flag_and_closes(factory) -> None:
    run_id = _seed_finished_run(factory)
    with _client(factory).stream("GET", f"/agent/runs/{run_id}/events") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = resp.read().decode()
    events = _messages(body)
    assert [(e["id"], e["event"], e["enforcement"]) for e in events] == [
        (1, "thread.started", "gated"),
        (2, "tool.requested", "observed"),
        (3, "permission.denied", "gated"),
        (4, "thread.failed", "observed"),
    ]
    assert all(e["observed_by"] == "forge" for e in events)
    assert [e["enforceable"] for e in events] == [True, False, True, False]


def test_last_event_id_and_after_resume_without_gaps_or_duplicates(factory) -> None:
    run_id = _seed_finished_run(factory)
    client = _client(factory)
    with client.stream("GET", f"/agent/runs/{run_id}/events", headers={"Last-Event-ID": "2"}) as r:
        assert [e["id"] for e in _messages(r.read().decode())] == [3, 4]
    with client.stream("GET", f"/agent/runs/{run_id}/events?after=3") as r:
        assert [e["id"] for e in _messages(r.read().decode())] == [4]


def test_follows_a_live_run_until_its_terminal_event(factory) -> None:
    run_id = _run(factory)
    _event(factory, run_id, 1, "thread.started", "gated")

    def finish() -> None:
        time.sleep(0.2)
        _event(factory, run_id, 2, "agent.message", "observed")
        _event(factory, run_id, 3, "thread.completed", "observed")

    worker = threading.Thread(target=finish)
    worker.start()
    with _client(factory).stream("GET", f"/agent/runs/{run_id}/events") as resp:
        body = resp.read().decode()
    worker.join()
    assert [e["event"] for e in _messages(body)] == [
        "thread.started",
        "agent.message",
        "thread.completed",
    ]


def test_a_finished_run_without_a_terminal_event_does_not_hang(factory) -> None:
    run_id = _run(factory, RunStatus.SUCCEEDED)
    _event(factory, run_id, 1, "thread.started", "gated")
    with _client(factory).stream("GET", f"/agent/runs/{run_id}/events") as resp:
        assert [e["id"] for e in _messages(resp.read().decode())] == [1]


def test_a_dishonest_stored_event_is_skipped(factory) -> None:
    run_id = _run(factory)
    _event(factory, run_id, 1, "agent.message", "gated")  # model text cannot be gated
    _event(factory, run_id, 2, "thread.completed", "observed")
    with _client(factory).stream("GET", f"/agent/runs/{run_id}/events") as resp:
        assert [e["id"] for e in _messages(resp.read().decode())] == [2]


def test_sends_keep_alives_while_waiting(factory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_events_router, "KEEPALIVE_SECONDS", 0.0)
    monkeypatch.setattr(run_events_router, "MAX_STREAM_SECONDS", 0.3)
    run_id = _run(factory)
    with _client(factory).stream("GET", f"/agent/runs/{run_id}/events") as resp:
        assert ": keepalive" in resp.read().decode()


def test_foreign_and_unknown_runs_are_404(factory) -> None:
    run_id = _seed_finished_run(factory)
    assert _client(factory, OTHER).get(f"/agent/runs/{run_id}/events").status_code == 404
    assert _client(factory).get(f"/agent/runs/{uuid.uuid4()}/events").status_code == 404
