"""Unit + Postgres-integration tests for the cassette-persistence slice
(``run_recording`` — the storage substrate for Time-Travel Runs).

The SQLite tests exercise the shared substrate (roundtrip, workspace scoping,
nullable run links, cassette-shape fidelity) the way ``test_models.py`` does
for every other model; the Postgres-marked tests exercise the real code path
the SQLite unit tests cannot: the F39 immutability trigger that blocks
UPDATE/DELETE on the append-only ``run_recording`` table. Uses the shared
``pg_engine`` fixture; skips (parked) without Postgres.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from forge_db.base import Base
from forge_db.models import AgentRun, Project, RunRecording, Task, WorkflowRun, Workspace


def _sample_cassette() -> dict[str, object]:
    return {
        "llm_calls": [
            {
                "index": 0,
                "request_digest": "a" * 64,
                "response": {"content": "hi", "model": "claude-x"},
                "model": "claude-x",
                "ts": 0.0,
            }
        ],
        "tool_calls": [
            {
                "index": 0,
                "name": "read_file",
                "args_digest": "b" * 64,
                "result": {"ok": True, "output": "file contents"},
                "ts": 1.0,
            }
        ],
        "env": {"MODEL": "claude-x"},
    }


def _content_hash(cassette: dict[str, object]) -> str:
    canonical = json.dumps(cassette, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# SQLite unit tests                                                            #
# --------------------------------------------------------------------------- #


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


def _seed(session: Session) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seed workspace -> project -> task -> workflow_run + agent_run."""
    ws = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    session.add(ws)
    session.flush()
    project = Project(workspace_id=ws.id, name="Core", key=f"C{uuid.uuid4().hex[:4]}")
    session.add(project)
    session.flush()
    task = Task(
        workspace_id=ws.id,
        project_id=project.id,
        key=f"TASK-{uuid.uuid4().hex[:6]}",
        title="time-travel task",
    )
    session.add(task)
    session.flush()
    run = WorkflowRun(workspace_id=ws.id, task_id=task.id)
    session.add(run)
    session.flush()
    agent = AgentRun(workspace_id=ws.id, workflow_run_id=run.id, task_id=task.id, model="claude-x")
    session.add(agent)
    session.flush()
    return ws.id, run.id, agent.id


def test_insert_and_roundtrip(sqlite_session: Session) -> None:
    ws_id, run_id, agent_id = _seed(sqlite_session)
    cassette = _sample_cassette()
    row = RunRecording(
        workspace_id=ws_id,
        agent_run_id=agent_id,
        workflow_run_id=run_id,
        cassette=cassette,
        model="claude-x",
        content_hash=_content_hash(cassette),
    )
    sqlite_session.add(row)
    sqlite_session.commit()
    row_id = row.id

    loaded = sqlite_session.get(RunRecording, row_id)
    assert loaded is not None
    assert loaded.workspace_id == ws_id
    assert loaded.agent_run_id == agent_id
    assert loaded.workflow_run_id == run_id
    assert loaded.model == "claude-x"
    assert loaded.content_hash == _content_hash(cassette)
    assert loaded.cassette == cassette
    assert loaded.cassette["llm_calls"][0]["model"] == "claude-x"
    assert loaded.cassette["tool_calls"][0]["name"] == "read_file"
    assert loaded.created_at is not None
    assert loaded.updated_at is not None


def test_run_ids_are_independently_nullable(sqlite_session: Session) -> None:
    ws_id, run_id, agent_id = _seed(sqlite_session)
    cassette = _sample_cassette()

    neither = RunRecording(
        workspace_id=ws_id,
        cassette=cassette,
        content_hash=_content_hash(cassette),
    )
    only_workflow = RunRecording(
        workspace_id=ws_id,
        workflow_run_id=run_id,
        cassette=cassette,
        content_hash=_content_hash(cassette),
    )
    only_agent = RunRecording(
        workspace_id=ws_id,
        agent_run_id=agent_id,
        cassette=cassette,
        content_hash=_content_hash(cassette),
    )
    sqlite_session.add_all([neither, only_workflow, only_agent])
    sqlite_session.commit()

    assert neither.agent_run_id is None and neither.workflow_run_id is None
    assert only_workflow.agent_run_id is None and only_workflow.workflow_run_id == run_id
    assert only_agent.agent_run_id == agent_id and only_agent.workflow_run_id is None


def test_model_is_nullable(sqlite_session: Session) -> None:
    ws_id, _run_id, _agent_id = _seed(sqlite_session)
    cassette = {"llm_calls": [], "tool_calls": [], "env": {}}
    row = RunRecording(
        workspace_id=ws_id,
        cassette=cassette,
        content_hash=_content_hash(cassette),
    )
    sqlite_session.add(row)
    sqlite_session.commit()
    assert row.model is None


def test_multiple_recordings_per_workspace(sqlite_session: Session) -> None:
    # A run may be re-recorded (retried/re-attempted); nothing on the model
    # constrains a workspace/run to a single cassette. Ordering "newest first"
    # by ``created_at`` is exercised on Postgres (see ``Attestation``'s
    # equivalent test) — SQLite's second-resolution timestamp can legitimately
    # tie two same-transaction-adjacent commits, so this unit test only asserts
    # both rows persist and are independently retrievable.
    ws_id, run_id, agent_id = _seed(sqlite_session)
    cassette = _sample_cassette()
    first = RunRecording(
        workspace_id=ws_id,
        workflow_run_id=run_id,
        agent_run_id=agent_id,
        cassette=cassette,
        content_hash="1" * 64,
    )
    second = RunRecording(
        workspace_id=ws_id,
        workflow_run_id=run_id,
        agent_run_id=agent_id,
        cassette=cassette,
        content_hash="2" * 64,
    )
    sqlite_session.add_all([first, second])
    sqlite_session.commit()

    rows = sqlite_session.scalars(
        select(RunRecording).where(RunRecording.workspace_id == ws_id)
    ).all()
    assert {r.content_hash for r in rows} == {"1" * 64, "2" * 64}


# --------------------------------------------------------------------------- #
# Postgres integration tests (immutability)                                   #
# --------------------------------------------------------------------------- #


@pytest.fixture
def factory(pg_engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    try:
        yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(pg_engine)


@pytest.mark.usefixtures("pg_engine")
def test_run_recording_is_immutable(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id, run_id, agent_id = _seed(session)
        cassette = _sample_cassette()
        row = RunRecording(
            workspace_id=ws_id,
            agent_run_id=agent_id,
            workflow_run_id=run_id,
            cassette=cassette,
            model="claude-x",
            content_hash=_content_hash(cassette),
        )
        session.add(row)
        session.commit()
        row_id = row.id

    # A direct UPDATE is blocked by the immutability trigger.
    with factory() as session:
        with pytest.raises((ProgrammingError, IntegrityError)):
            session.execute(
                update(RunRecording).where(RunRecording.id == row_id).values(model="tampered")
            )
            session.commit()
        session.rollback()

    # A direct DELETE is likewise blocked.
    with factory() as session:
        with pytest.raises((ProgrammingError, IntegrityError)):
            session.execute(text("DELETE FROM run_recording WHERE id = :i"), {"i": str(row_id)})
            session.commit()
        session.rollback()


@pytest.mark.usefixtures("pg_engine")
def test_deleting_a_linked_run_is_blocked_by_immutability(factory: sessionmaker[Session]) -> None:
    """Mirrors ``Attestation``'s equivalent test: ``ON DELETE CASCADE`` would
    DELETE the recording row, but the append-only trigger intercepts that
    DELETE just like a direct one — so a run that has been recorded cannot be
    hard-deleted at all (fail-closed, same accepted tension documented on
    ``Attestation``)."""
    with factory() as session:
        ws_id, run_id, agent_id = _seed(session)
        cassette = _sample_cassette()
        session.add(
            RunRecording(
                workspace_id=ws_id,
                agent_run_id=agent_id,
                workflow_run_id=run_id,
                cassette=cassette,
                content_hash=_content_hash(cassette),
            )
        )
        session.commit()

    with factory() as session:
        run = session.get(WorkflowRun, run_id)
        assert run is not None
        session.delete(run)
        with pytest.raises((ProgrammingError, IntegrityError)):
            session.commit()
        session.rollback()
