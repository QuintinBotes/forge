"""Open run states (spec K §4.3): line state names persist, old payloads still load."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session, sessionmaker
from temporalio.api.common.v1 import Payload
from temporalio.contrib.pydantic import pydantic_data_converter

from forge_contracts import RunStatus, WorkflowRun, WorkflowState
from forge_db.base import Base
from forge_db.models import Project, Task, Workspace
from forge_workflow.store import SqlAlchemyWorkflowStore
from forge_workflow.temporal.activities import WorkflowActivities
from forge_workflow.temporal.payloads import TransitionRecord

# Encoded by the pre-change TransitionRecord (from_state/to_state: WorkflowState).
_PRE_CHANGE_PAYLOAD = Payload(
    metadata={"encoding": b"json/plain"},
    data=(
        b'{"workflow_run_id":"11111111-1111-1111-1111-111111111111",'
        b'"workspace_id":"22222222-2222-2222-2222-222222222222",'
        b'"from_state":"spec_review","to_state":"spec_approved",'
        b'"event":"spec_approved_by_human",'
        b'"idempotency_key":"11111111-1111-1111-1111-111111111111:transition:3",'
        b'"guard_results":{},"effects_dispatched":["notify"],"record":null,'
        b'"actor":"user:alice","skill":null,"payload":{},"temporal_run_id":"run-1"}'
    ),
)


def test_pre_change_transition_record_payload_still_loads() -> None:
    converter = pydantic_data_converter.payload_converter
    (rec,) = converter.from_payloads([_PRE_CHANGE_PAYLOAD], [TransitionRecord])
    assert isinstance(rec, TransitionRecord)
    assert (rec.from_state, rec.to_state) == ("spec_review", "spec_approved")
    assert rec.run_status is None
    assert rec.temporal_run_id == "run-1"


def test_feature_record_encodes_like_before() -> None:
    """FeatureWorkflow still passes WorkflowState members; the JSON is unchanged."""
    rec = TransitionRecord(
        workflow_run_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        workspace_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
        from_state=WorkflowState.SPEC_REVIEW,
        to_state=WorkflowState.SPEC_APPROVED,
        event="spec_approved_by_human",
        idempotency_key="11111111-1111-1111-1111-111111111111:transition:3",
        effects_dispatched=["notify"],
        actor="user:alice",
        temporal_run_id="run-1",
    )
    (payload,) = pydantic_data_converter.payload_converter.to_payloads([rec])
    old = _PRE_CHANGE_PAYLOAD.data
    assert payload.data == old[:-1] + b',"run_status":null}'


@pytest.fixture
def factory(pg_engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    try:
        yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(pg_engine)


def _seed_task(session: Session) -> tuple[uuid.UUID, uuid.UUID]:
    ws = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    session.add(ws)
    session.flush()
    project = Project(workspace_id=ws.id, name="Core", key=f"C{uuid.uuid4().hex[:4]}")
    session.add(project)
    session.flush()
    task = Task(workspace_id=ws.id, project_id=project.id, key="T-1", title="line task")
    session.add(task)
    session.flush()
    return ws.id, task.id


@pytest.mark.asyncio
async def test_line_state_round_trips_through_store_and_projection(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        ws_id, task_id = _seed_task(session)
        store = SqlAlchemyWorkflowStore(session, workspace_id=ws_id)
        run = store.create(
            WorkflowRun(task_id=task_id, current_state="implement", status=RunStatus.RUNNING)
        )
        assert run.id is not None
        session.commit()
        session.expunge_all()  # the next read comes from Postgres, not the identity map

        assert store.get(run.id).current_state == "implement"

        rec = TransitionRecord(
            workflow_run_id=run.id,
            workspace_id=ws_id,
            from_state="implement",
            to_state="review_changes",
            event="succeeded",
            idempotency_key=f"{run.id}:transition:1",
        )
        assert await WorkflowActivities(store=store).persist_transition(rec) == 1
        session.commit()
        session.expunge_all()

        reloaded = store.get(run.id)
        assert reloaded.current_state == "review_changes"
        assert reloaded.status == RunStatus.RUNNING
        (transition,) = reloaded.context["transitions"]
        assert (transition["from_state"], transition["to_state"]) == (
            "implement",
            "review_changes",
        )
