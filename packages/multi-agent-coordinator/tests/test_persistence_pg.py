"""``sub_agent_run`` persistence against real Postgres (AC 15, 20 at-rest)."""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.postgres


@pytest.fixture
def session_factory(pg_engine):
    from sqlalchemy.orm import Session

    from forge_db.base import Base

    Base.metadata.create_all(pg_engine)

    def _factory() -> Session:
        return Session(pg_engine)

    return _factory


def _seed_parent(session_factory) -> tuple[uuid.UUID, uuid.UUID]:
    from forge_db.models import AgentRun, Workspace

    ws_id = uuid.uuid4()
    parent_id = uuid.uuid4()
    with session_factory() as session:
        session.add(Workspace(id=ws_id, name="WS", slug=f"ws-{ws_id.hex[:8]}"))
        session.commit()
    with session_factory() as session:
        session.add(
            AgentRun(
                id=parent_id,
                workspace_id=ws_id,
                role="primary",
                is_supervisor=True,
                pattern="maker_checker",
            )
        )
        session.commit()
    return ws_id, parent_id


def test_sub_agent_run_create_update_list_roundtrip(session_factory) -> None:
    from forge_contracts import RunStatus, SubAgentRole
    from forge_coordinator import SqlAlchemySubAgentRunSink, SubAgentRunCreate

    ws_id, parent_id = _seed_parent(session_factory)
    sink = SqlAlchemySubAgentRunSink(session_factory)

    row_id = sink.create(
        SubAgentRunCreate(
            parent_agent_run_id=parent_id,
            workspace_id=ws_id,
            assignment_id="sa-implementer-1",
            role=SubAgentRole.IMPLEMENTER,
            pattern="maker_checker",
            ordinal=0,
            objective={"objective": "build", "model": {"api_key": "sk-leak-me"}},
            status=RunStatus.RUNNING,
        )
    )

    # The child F06 run row must exist (FK target).
    from forge_db.models import AgentRun

    child_run_id = uuid.uuid4()
    with session_factory() as session:
        session.add(AgentRun(id=child_run_id, workspace_id=ws_id, role="implementer"))
        session.commit()

    sink.update(
        row_id,
        status=RunStatus.SUCCEEDED,
        artifact={"kind": "code_change", "summary": "done", "branch_name": "forge/sa-impl"},
        confidence=0.9,
        branch_name="forge/sa-impl",
        merged=True,
        token_usage={"input_tokens": 10, "output_tokens": 5},
        agent_run_id=child_run_id,
    )

    results = sink.list_for_parent(parent_id)
    assert len(results) == 1
    res = results[0]
    assert res.assignment_id == "sa-implementer-1"
    assert res.role is SubAgentRole.IMPLEMENTER
    assert res.status == "succeeded"
    assert res.confidence == 0.9
    assert res.agent_run_id == child_run_id
    assert res.token_usage.input_tokens == 10

    # AC 20 (at rest): the model api key never lands in the persisted objective.
    from forge_db.models import SubAgentRun

    with session_factory() as session:
        obj = session.get(SubAgentRun, row_id)
        assert obj is not None
        assert "sk-leak-me" not in str(obj.objective)
        assert obj.merged is True
        assert obj.agent_run_id == child_run_id


def test_unique_assignment_per_parent(session_factory) -> None:
    from sqlalchemy.exc import IntegrityError

    from forge_contracts import RunStatus, SubAgentRole
    from forge_coordinator import SqlAlchemySubAgentRunSink, SubAgentRunCreate

    ws_id, parent_id = _seed_parent(session_factory)
    sink = SqlAlchemySubAgentRunSink(session_factory)
    base = SubAgentRunCreate(
        parent_agent_run_id=parent_id,
        workspace_id=ws_id,
        assignment_id="sa-implementer-1",
        role=SubAgentRole.IMPLEMENTER,
        pattern="maker_checker",
        ordinal=0,
        status=RunStatus.RUNNING,
    )
    sink.create(base)
    with pytest.raises(IntegrityError):
        sink.create(base)
