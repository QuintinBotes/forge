"""Integration tests for the F21 automation worker (evaluate + sweep).

Hermetic: in-memory SQLite (StaticPool) seeded with a workspace/project/spec +
linked tasks. Covers the canonical close-spec firing, condition gate,
idempotency, partial failure, loop-skip, and the sweeper backstop
(ACs 4, 5, 7, 8, 13, 15, 18).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from forge_board.automation import (
    AutomationRuleSpec,
    CloseLinkedSpecTasksAction,
    Condition,
    ConditionGroup,
    SetPriorityAction,
    SetStatusAction,
    TriggerSpec,
)
from forge_contracts.automation import (
    AutomationExecutionStatus,
    AutomationTriggerEnvelope,
    AutomationTriggerSource,
    AutomationTriggerType,
    ConditionOp,
)
from forge_contracts.enums import Priority, TaskStatus
from forge_db.base import Base
from forge_db.models import (
    AutomationExecution,
    AutomationRule,
    Project,
    SpecDocument,
    Task,
    Workspace,
)
from forge_worker.tasks.automations import (
    evaluate_envelope,
    sweep_unprocessed_triggers,
)

WS_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def _seed(factory: sessionmaker[Session], *, with_spec: bool = True) -> dict[str, uuid.UUID]:
    with factory() as session:
        session.add(Workspace(id=WS_ID, name="Acme", slug="acme"))
        session.flush()
        project = Project(workspace_id=WS_ID, name="Core", key="CORE")
        session.add(project)
        session.flush()
        spec_id = None
        if with_spec:
            spec = SpecDocument(
                workspace_id=WS_ID, project_id=project.id, spec_key="SPEC-17", name="S"
            )
            session.add(spec)
            session.flush()
            spec_id = spec.id
        ids: dict[str, uuid.UUID] = {"project": project.id}
        if spec_id:
            ids["spec"] = spec_id
        for i, status in enumerate(
            [TaskStatus.IN_REVIEW, TaskStatus.IN_PROGRESS, TaskStatus.BACKLOG]
        ):
            t = Task(
                workspace_id=WS_ID,
                project_id=project.id,
                spec_id=spec_id,
                key=f"CORE-{i + 1}",
                title=f"task {i}",
                status=status,
            )
            session.add(t)
            session.flush()
            ids[f"task{i}"] = t.id
        session.commit()
    return ids


def _add_rule(
    factory: sessionmaker[Session], spec: AutomationRuleSpec, project_id: uuid.UUID
) -> uuid.UUID:
    with factory() as session:
        row = AutomationRule(
            workspace_id=WS_ID,
            project_id=project_id,
            name=spec.name,
            enabled=spec.enabled,
            trigger_type=spec.trigger.type,
            trigger_config=spec.trigger.config,
            condition=spec.condition.model_dump(),
            actions=[a.model_dump(mode="json") for a in spec.actions],
            run_order=spec.run_order,
        )
        session.add(row)
        session.commit()
        return row.id


def _merged_envelope(task_id: uuid.UUID, project_id: uuid.UUID, **kw) -> AutomationTriggerEnvelope:
    return AutomationTriggerEnvelope(
        trigger_type=AutomationTriggerType.WORKFLOW_STATE_CHANGED,
        trigger_source=AutomationTriggerSource.WORKFLOW_TRANSITION,
        trigger_event_id=kw.pop("event_id", uuid.uuid4()),
        workspace_id=WS_ID,
        project_id=project_id,
        entity_id=task_id,
        change={"to_state": "merged"},
        **kw,
    )


def _close_rule() -> AutomationRuleSpec:
    return AutomationRuleSpec(
        name="Close spec tasks on merge",
        trigger=TriggerSpec(
            type=AutomationTriggerType.WORKFLOW_STATE_CHANGED, config={"to_state": "merged"}
        ),
        condition=ConditionGroup(
            conditions=[Condition(field="has_spec", op=ConditionOp.EQ, value=True)]
        ),
        actions=[CloseLinkedSpecTasksAction()],
    )


def test_canonical_close_spec_fires(factory) -> None:
    ids = _seed(factory)
    _add_rule(factory, _close_rule(), ids["project"])
    env = _merged_envelope(ids["task0"], ids["project"])

    rows = evaluate_envelope(factory, env)
    assert len(rows) == 1
    assert rows[0].status is AutomationExecutionStatus.SUCCEEDED
    closed = rows[0].action_results[0]["detail"]["closed_task_ids"]
    assert set(closed) == {str(ids["task1"]), str(ids["task2"])}

    with factory() as session:
        # Trigger task NOT closed; siblings closed.
        assert session.get(Task, ids["task0"]).status == TaskStatus.IN_REVIEW
        assert session.get(Task, ids["task1"]).status == TaskStatus.DONE
        assert session.get(Task, ids["task2"]).status == TaskStatus.DONE


def test_condition_gate_blocks_mutation(factory) -> None:
    ids = _seed(factory, with_spec=False)
    _add_rule(factory, _close_rule(), ids["project"])
    env = _merged_envelope(ids["task0"], ids["project"])

    rows = evaluate_envelope(factory, env)
    assert rows[0].status is AutomationExecutionStatus.CONDITIONS_FAILED
    with factory() as session:
        for key in ("task1", "task2"):
            assert session.get(Task, ids[key]).status != TaskStatus.DONE


def test_idempotent_redelivery(factory) -> None:
    ids = _seed(factory)
    _add_rule(factory, _close_rule(), ids["project"])
    event_id = uuid.uuid4()
    env = _merged_envelope(ids["task0"], ids["project"], event_id=event_id)

    first = evaluate_envelope(factory, env)
    assert len(first) == 1
    # Same event id again -> nothing new (skipped before actions re-run).
    again = _merged_envelope(ids["task0"], ids["project"], event_id=event_id)
    second = evaluate_envelope(factory, again)
    assert second == []
    with factory() as session:
        rows = list(session.execute(select(AutomationExecution)).scalars())
        assert len(rows) == 1


def test_partial_failure_isolation(factory) -> None:
    ids = _seed(factory)
    # set_status on the IN_REVIEW trigger task to an illegal target -> error;
    # set_priority -> ok. Result: partial_failure with the priority applied.
    spec = AutomationRuleSpec(
        name="partial",
        trigger=TriggerSpec(
            type=AutomationTriggerType.WORKFLOW_STATE_CHANGED, config={"to_state": "merged"}
        ),
        actions=[
            SetStatusAction(status=TaskStatus.BACKLOG),  # illegal from in_review
            SetPriorityAction(priority=Priority.URGENT),
        ],
    )
    _add_rule(factory, spec, ids["project"])
    rows = evaluate_envelope(factory, _merged_envelope(ids["task0"], ids["project"]))
    assert rows[0].status is AutomationExecutionStatus.PARTIAL_FAILURE
    statuses = {r["status"] for r in rows[0].action_results}
    assert "error" in statuses and "ok" in statuses
    with factory() as session:
        assert session.get(Task, ids["task0"]).priority == Priority.URGENT


def test_loop_guard_skips_at_depth(factory) -> None:
    ids = _seed(factory)
    _add_rule(factory, _close_rule(), ids["project"])
    env = _merged_envelope(ids["task0"], ids["project"], depth=5)
    rows = evaluate_envelope(factory, env, max_depth=5)
    assert rows[0].status is AutomationExecutionStatus.SKIPPED_LOOP
    with factory() as session:
        assert session.get(Task, ids["task1"]).status != TaskStatus.DONE


def test_audit_fanout_on_mutating_firing(factory) -> None:
    ids = _seed(factory)
    _add_rule(factory, _close_rule(), ids["project"])
    captured: list = []
    evaluate_envelope(
        factory, _merged_envelope(ids["task0"], ids["project"]), audit_sink=captured.append
    )
    assert len(captured) == 1  # one state-mutating firing fanned out


def test_sweeper_redispatches_unprocessed(factory) -> None:
    ids = _seed(factory)
    _add_rule(factory, _close_rule(), ids["project"])
    env = _merged_envelope(ids["task0"], ids["project"])

    class RecordingDispatcher:
        def __init__(self) -> None:
            self.dispatched: list = []

        def dispatch(self, e: AutomationTriggerEnvelope) -> None:
            self.dispatched.append(e)

    disp = RecordingDispatcher()
    # No execution exists yet -> the sweeper re-dispatches it.
    n = sweep_unprocessed_triggers(factory, [env], dispatcher=disp)
    assert n == 1 and len(disp.dispatched) == 1

    # After processing, the sweeper no longer re-dispatches.
    evaluate_envelope(factory, env)
    disp2 = RecordingDispatcher()
    n2 = sweep_unprocessed_triggers(factory, [env], dispatcher=disp2)
    assert n2 == 0 and disp2.dispatched == []
