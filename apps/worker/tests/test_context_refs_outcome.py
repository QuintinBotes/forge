"""The worker copies a run's ``context_refs`` into its result artifacts (spec J §2.5)."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from forge_contracts import AgentRunResult, RunStatus
from forge_db.base import Base
from forge_db.models import AgentRun, Workspace
from forge_db.models.enums import RunStatus as DbRunStatus
from forge_worker.agent_runner import _record_run_outcome

WS = uuid.UUID("00000000-0000-0000-0000-00000000aa77")
REFS = [{"kind": "fact", "id": "f-1"}, {"kind": "memory", "id": "m-1"}]


@pytest.fixture
def session_factory() -> Iterator[Callable[[], Session]]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Workspace(id=WS, name="w", slug="w"))
        session.commit()
    yield lambda: Session(engine)
    engine.dispose()


def test_context_refs_copied_into_artifacts(session_factory: Callable[[], Session]) -> None:
    run_id = uuid.uuid4()
    with session_factory() as session:
        session.add(
            AgentRun(
                id=run_id, workspace_id=WS, status=DbRunStatus.PENDING, context_refs=list(REFS)
            )
        )
        session.commit()
    result = AgentRunResult(run_id=run_id, status=RunStatus.SUCCEEDED, output="ok")
    _record_run_outcome(run_id, WS, result, session_factory=session_factory)
    assert result.artifacts["context_refs"] == REFS
    with session_factory() as session:
        row = session.get(AgentRun, run_id)
        assert row is not None and row.output is not None
        assert row.output["artifacts"]["context_refs"] == REFS


def test_no_context_refs_leaves_artifacts_alone(
    session_factory: Callable[[], Session],
) -> None:
    run_id = uuid.uuid4()
    with session_factory() as session:
        session.add(AgentRun(id=run_id, workspace_id=WS, status=DbRunStatus.PENDING))
        session.commit()
    result = AgentRunResult(run_id=run_id, status=RunStatus.SUCCEEDED, output="ok")
    _record_run_outcome(run_id, WS, result, session_factory=session_factory)
    assert "context_refs" not in result.artifacts
