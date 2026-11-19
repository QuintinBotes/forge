"""``forge.persist_station`` (spec K §4.3, §4.11): one row per station visit.

The activity upserts the visit by its id, never moves its status backward, redacts
and caps what it stores, and mirrors outputs, spend and ``parked`` into the run's
context. Runs on SQLite, and on Postgres when one is configured.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from temporalio.exceptions import ApplicationError

from scf_contracts import RunStatus, WorkflowRun
from scf_db.base import Base
from scf_db.models import Project, Task, Workspace
from scf_db.models.runs import WorkflowStationVisit
from scf_workflow.store import InMemoryWorkflowStore, SqlAlchemyWorkflowStore
from scf_workflow.temporal.activities import WorkflowActivities
from scf_workflow.temporal.ids import station_visit_id
from scf_workflow.temporal.payloads import StationVisitRecord

pytestmark = pytest.mark.asyncio

REVISION = uuid.UUID("33333333-3333-3333-3333-333333333333")


@pytest.fixture(params=["sqlite", "postgres"])
def factory(request: pytest.FixtureRequest) -> Iterator[sessionmaker[Session]]:
    if request.param == "postgres":
        engine = request.getfixturevalue("pg_engine")
    else:
        # One shared connection: the activity body runs on a worker thread.
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(engine)


def _seed_run(session: Session) -> tuple[SqlAlchemyWorkflowStore, uuid.UUID, uuid.UUID]:
    ws = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    session.add(ws)
    session.flush()
    project = Project(workspace_id=ws.id, name="Core", key=f"C{uuid.uuid4().hex[:4]}")
    session.add(project)
    session.flush()
    task = Task(workspace_id=ws.id, project_id=project.id, key="T-1", title="line task")
    session.add(task)
    session.flush()
    store = SqlAlchemyWorkflowStore(session, workspace_id=ws.id)
    run = store.create(
        WorkflowRun(task_id=task.id, current_state="implement", status=RunStatus.RUNNING)
    )
    assert run.id is not None
    session.commit()
    return store, ws.id, run.id


def _visit(run_id: uuid.UUID, ws_id: uuid.UUID, status: str, **extra: object) -> StationVisitRecord:
    fields: dict[str, object] = {"state": "implement", "visit": 1, "kind": "agent"}
    fields.update(extra)
    return StationVisitRecord(
        workflow_run_id=run_id,
        workspace_id=ws_id,
        definition_revision_id=REVISION,
        status=status,
        **fields,  # type: ignore[arg-type]
    )


def _rows(session: Session, run_id: uuid.UUID) -> list[WorkflowStationVisit]:
    session.expunge_all()  # read what the database holds, not the identity map
    stmt = select(WorkflowStationVisit).where(WorkflowStationVisit.workflow_run_id == run_id)
    return list(session.scalars(stmt.order_by(WorkflowStationVisit.started_at)))


async def test_upserts_one_row_per_visit_and_status_only_moves_forward(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        store, ws_id, run_id = _seed_run(session)
        acts = WorkflowActivities(store=store)
        agent_run = uuid.uuid4()

        running = _visit(run_id, ws_id, "running", agent_run_id=agent_run)
        visit_id = await acts.persist_station(running)
        session.commit()
        assert visit_id == str(uuid.uuid5(run_id, "implement#1"))
        assert visit_id == str(station_visit_id(run_id, "implement", 1))
        (row,) = _rows(session, run_id)
        assert (row.id, row.status, row.finished_at) == (uuid.UUID(visit_id), "running", None)
        assert (row.workspace_id, row.definition_revision_id) == (ws_id, REVISION)

        done = _visit(
            run_id,
            ws_id,
            "succeeded",
            outcome="succeeded",
            outputs={"summary": "done"},
            tokens_used=120,
            cost_usd=0.25,
            priced=True,
        )
        await acts.persist_station(done)
        session.commit()
        (row,) = _rows(session, run_id)
        assert (row.status, row.outcome) == ("succeeded", "succeeded")
        assert row.outputs == {"summary": "done"}
        assert (row.tokens_used, float(row.cost_usd or 0), row.priced) == (120, 0.25, True)
        assert row.agent_run_id == agent_run  # a later report without the link keeps it
        assert row.finished_at is not None
        finished_at = row.finished_at

        # A late retry of ``running`` (or of ``succeeded``) changes nothing.
        await acts.persist_station(running)
        await acts.persist_station(_visit(run_id, ws_id, "failed", outcome="failed"))
        session.commit()
        (row,) = _rows(session, run_id)
        assert (row.status, row.outcome) == ("succeeded", "succeeded")
        assert row.outputs == {"summary": "done"}
        assert row.finished_at == finished_at


async def test_outputs_and_error_message_are_redacted_and_outputs_capped(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        store, ws_id, run_id = _seed_run(session)
        acts = WorkflowActivities(store=store)

        await acts.persist_station(
            _visit(
                run_id,
                ws_id,
                "failed",
                outputs={"token": "s3cr3t-value", "log": ["Authorization: Bearer abc.def.ghi"]},
                error_message="push failed: password=hunter2",
            )
        )
        await acts.persist_station(
            _visit(run_id, ws_id, "succeeded", visit=2, outputs={"diff": "x" * 20_000})
        )
        session.commit()
        first, second = _rows(session, run_id)
        stored = json.dumps(first.outputs) + (first.error_message or "")
        assert "s3cr3t-value" not in stored
        assert "abc.def.ghi" not in stored
        assert "hunter2" not in stored
        assert first.outputs["token"] == "[redacted]"
        assert len(json.dumps(second.outputs).encode()) <= 16 * 1024
        assert second.outputs["truncated"] is True


async def test_mirrors_latest_outputs_spend_and_parked_into_the_run_context(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        store, ws_id, run_id = _seed_run(session)
        acts = WorkflowActivities(store=store)

        await acts.persist_station(
            _visit(
                run_id,
                ws_id,
                "succeeded",
                state="plan",
                outputs={"plan": "v1"},
                tokens_used=100,
                cost_usd=0.1,
                priced=True,
            )
        )
        parked = _visit(
            run_id,
            ws_id,
            "parked",
            outcome="low_confidence",
            outputs={"note": "unsure"},
            tokens_used=50,
            priced=False,
        )
        await acts.persist_station(parked)
        await acts.persist_station(parked)  # a retry does not count the spend twice
        session.commit()
        session.expunge_all()

        context = store.get(run_id).context
        assert context["outputs"] == {"plan": {"plan": "v1"}, "implement": {"note": "unsure"}}
        assert context["spend"] == {"tokens": 150, "usd": 0.1, "unpriced_tokens": 50}
        assert context["parked"] == {"state": "implement", "visit": 1, "reason": "low_confidence"}

        # Resumed: the station's next visit runs, so the run is no longer parked.
        await acts.persist_station(_visit(run_id, ws_id, "running", visit=2))
        session.commit()
        session.expunge_all()
        context = store.get(run_id).context
        assert "parked" not in context
        assert context["outputs"]["implement"] == {}
        assert store.get(run_id).status == RunStatus.RUNNING  # persist_transition owns status


async def test_refuses_a_store_that_cannot_record_visits() -> None:
    store = InMemoryWorkflowStore()
    run = store.create(WorkflowRun(task_id=uuid.uuid4(), current_state="implement"))
    assert run.id is not None
    with pytest.raises(ApplicationError) as exc:
        await WorkflowActivities(store=store).persist_station(
            _visit(run.id, uuid.uuid4(), "running")
        )
    assert exc.value.type == "ActivityNotConfigured"
    assert exc.value.non_retryable


async def test_refuses_an_unknown_status_without_retrying(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        store, ws_id, run_id = _seed_run(session)
        with pytest.raises(ApplicationError) as exc:
            await WorkflowActivities(store=store).persist_station(_visit(run_id, ws_id, "done"))
        assert exc.value.non_retryable
        assert _rows(session, run_id) == []


async def test_a_foreign_workspace_store_cannot_write_the_visit(
    factory: sessionmaker[Session],
) -> None:
    from scf_workflow import WorkflowRunNotFoundError

    with factory() as session:
        _, ws_id, run_id = _seed_run(session)
        foreign = SqlAlchemyWorkflowStore(session, workspace_id=uuid.uuid4())
        with pytest.raises(WorkflowRunNotFoundError):
            await WorkflowActivities(store=foreign).persist_station(
                _visit(run_id, ws_id, "running")
            )
        assert _rows(session, run_id) == []


def test_persist_station_is_registered() -> None:
    names = {
        fn.__temporal_activity_definition.name  # type: ignore[attr-defined]
        for fn in WorkflowActivities(allow_fakes=True).register()
    }
    assert "forge.persist_station" in names
