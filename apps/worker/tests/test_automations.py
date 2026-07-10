"""Integration tests for the F21 automation worker (evaluate + sweep).

Hermetic: in-memory SQLite (StaticPool) seeded with a workspace/project/spec +
linked tasks. Covers the canonical close-spec firing, condition gate,
idempotency, partial failure, loop-skip, and the sweeper backstop
(ACs 4, 5, 7, 8, 13, 15, 18).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

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
    TaskDependency,
    Workspace,
)
from forge_worker.tasks.automations import (
    cron_matches,
    dispatch_due_scheduled_automations,
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


# --------------------------------------------------------------------------- #
# F40: scheduled (cron) trigger                                               #
# --------------------------------------------------------------------------- #


class _RecordingDispatcher:
    def __init__(self) -> None:
        self.dispatched: list[AutomationTriggerEnvelope] = []

    def dispatch(self, e: AutomationTriggerEnvelope) -> None:
        self.dispatched.append(e)


def test_cron_matches_fake_clock() -> None:
    """The cron matcher is pure: due-ness is decided by the supplied clock."""
    # 09:00 on weekdays (Mon-Fri). 2026-07-10 is a Friday.
    expr = "0 9 * * 1-5"
    assert cron_matches(expr, datetime(2026, 7, 10, 9, 0, tzinfo=UTC)) is True
    assert cron_matches(expr, datetime(2026, 7, 10, 9, 1, tzinfo=UTC)) is False  # wrong minute
    assert cron_matches(expr, datetime(2026, 7, 10, 8, 0, tzinfo=UTC)) is False  # wrong hour
    # 2026-07-11 is a Saturday -> outside the Mon-Fri day-of-week set.
    assert cron_matches(expr, datetime(2026, 7, 11, 9, 0, tzinfo=UTC)) is False
    # Step + naive clock (treated as UTC).
    assert cron_matches("*/15 * * * *", datetime(2026, 7, 10, 12, 30)) is True
    assert cron_matches("*/15 * * * *", datetime(2026, 7, 10, 12, 31)) is False


def _scheduled_rule() -> AutomationRuleSpec:
    return AutomationRuleSpec(
        name="Nightly priority bump",
        trigger=TriggerSpec(type=AutomationTriggerType.SCHEDULED, config={"cron": "0 9 * * *"}),
        actions=[SetPriorityAction(priority=Priority.HIGH)],
    )


def test_scheduled_dispatch_fires_when_due(factory) -> None:
    ids = _seed(factory)
    _add_rule(factory, _scheduled_rule(), ids["project"])
    disp = _RecordingDispatcher()

    # Not due at 08:00 -> nothing dispatched.
    none = dispatch_due_scheduled_automations(
        factory, now=datetime(2026, 7, 10, 8, 0, tzinfo=UTC), dispatcher=disp
    )
    assert none == 0 and disp.dispatched == []

    # Due at 09:00 -> one envelope per in-scope task (3 seeded tasks).
    due = dispatch_due_scheduled_automations(
        factory, now=datetime(2026, 7, 10, 9, 0, tzinfo=UTC), dispatcher=disp
    )
    assert due == 3
    assert len(disp.dispatched) == 3
    env = disp.dispatched[0]
    assert env.trigger_type is AutomationTriggerType.SCHEDULED
    assert env.trigger_source is AutomationTriggerSource.SCHEDULER


def test_scheduled_dispatch_is_deterministic_per_minute(factory) -> None:
    """Same Beat minute -> identical trigger_event_ids (idempotent redelivery)."""
    ids = _seed(factory)
    _add_rule(factory, _scheduled_rule(), ids["project"])
    now = datetime(2026, 7, 10, 9, 0, tzinfo=UTC)

    a = _RecordingDispatcher()
    b = _RecordingDispatcher()
    dispatch_due_scheduled_automations(factory, now=now, dispatcher=a)
    dispatch_due_scheduled_automations(factory, now=now, dispatcher=b)

    assert {e.trigger_event_id for e in a.dispatched} == {e.trigger_event_id for e in b.dispatched}


def test_scheduled_dispatch_then_evaluate_applies_action(factory) -> None:
    """End-to-end: a due scheduled rule dispatches, and evaluation applies it."""
    ids = _seed(factory)
    _add_rule(factory, _scheduled_rule(), ids["project"])
    disp = _RecordingDispatcher()
    dispatch_due_scheduled_automations(
        factory, now=datetime(2026, 7, 10, 9, 0, tzinfo=UTC), dispatcher=disp
    )
    # Evaluate one of the dispatched envelopes through the normal engine path.
    target = next(e for e in disp.dispatched if e.entity_id == ids["task0"])
    rows = evaluate_envelope(factory, target)
    assert rows and rows[0].status is AutomationExecutionStatus.SUCCEEDED
    with factory() as session:
        assert session.get(Task, ids["task0"]).priority == Priority.HIGH


def test_scheduled_dispatch_isolates_malformed_cron(factory) -> None:
    """A malformed cron in ONE rule never aborts the whole minute's dispatch."""
    ids = _seed(factory)
    _add_rule(factory, _scheduled_rule(), ids["project"])  # valid "0 9 * * *"
    broken = AutomationRuleSpec(
        name="broken cron",
        trigger=TriggerSpec(type=AutomationTriggerType.SCHEDULED, config={"cron": "not a cron"}),
        actions=[SetPriorityAction(priority=Priority.HIGH)],
    )
    _add_rule(factory, broken, ids["project"])

    disp = _RecordingDispatcher()
    # The malformed rule raises inside cron_matches — it must be skipped, and the
    # valid rule must still fan out to all 3 seeded tasks (fault isolation).
    due = dispatch_due_scheduled_automations(
        factory, now=datetime(2026, 7, 10, 9, 0, tzinfo=UTC), dispatcher=disp
    )
    assert due == 3
    assert len(disp.dispatched) == 3


# --------------------------------------------------------------------------- #
# F40: aggregate conditions over the real task_dependency graph               #
# --------------------------------------------------------------------------- #


def _add_edge(factory, task_id: uuid.UUID, depends_on_id: uuid.UUID) -> None:
    with factory() as session:
        session.add(
            TaskDependency(workspace_id=WS_ID, task_id=task_id, depends_on_id=depends_on_id)
        )
        session.commit()


def _set_status(factory, task_id: uuid.UUID, status: TaskStatus) -> None:
    with factory() as session:
        session.get(Task, task_id).status = status
        session.commit()


def _aggregate_rule() -> AutomationRuleSpec:
    return AutomationRuleSpec(
        name="Bump when all subtasks done",
        trigger=TriggerSpec(
            type=AutomationTriggerType.WORKFLOW_STATE_CHANGED, config={"to_state": "merged"}
        ),
        condition=ConditionGroup(
            conditions=[Condition(field="all_subtasks_done", op=ConditionOp.EQ, value=True)]
        ),
        actions=[SetPriorityAction(priority=Priority.URGENT)],
    )


def test_aggregate_condition_fires_when_all_deps_done(factory) -> None:
    """all_subtasks_done resolves over the real task_dependency edges (DB path)."""
    ids = _seed(factory)
    _add_rule(factory, _aggregate_rule(), ids["project"])
    # task0 depends on task1 + task2; drive both to terminal statuses.
    _add_edge(factory, ids["task0"], ids["task1"])
    _add_edge(factory, ids["task0"], ids["task2"])
    _set_status(factory, ids["task1"], TaskStatus.DONE)
    _set_status(factory, ids["task2"], TaskStatus.CANCELLED)

    rows = evaluate_envelope(factory, _merged_envelope(ids["task0"], ids["project"]))
    assert rows and rows[0].status is AutomationExecutionStatus.SUCCEEDED
    with factory() as session:
        assert session.get(Task, ids["task0"]).priority == Priority.URGENT


def test_aggregate_condition_blocks_when_a_dep_open(factory) -> None:
    """One open dependency -> all_subtasks_done is False -> action is gated."""
    ids = _seed(factory)
    _add_rule(factory, _aggregate_rule(), ids["project"])
    _add_edge(factory, ids["task0"], ids["task1"])  # task1 seeded IN_PROGRESS (open)
    _add_edge(factory, ids["task0"], ids["task2"])
    _set_status(factory, ids["task2"], TaskStatus.DONE)

    rows = evaluate_envelope(factory, _merged_envelope(ids["task0"], ids["project"]))
    assert rows and rows[0].status is AutomationExecutionStatus.CONDITIONS_FAILED
    with factory() as session:
        # Priority untouched — the gate held (would have been URGENT if it fired).
        assert session.get(Task, ids["task0"]).priority != Priority.URGENT


def test_aggregate_condition_no_deps_is_vacuously_done(factory) -> None:
    """A task with no dependency edges has all_subtasks_done=True (fires)."""
    ids = _seed(factory)
    _add_rule(factory, _aggregate_rule(), ids["project"])
    rows = evaluate_envelope(factory, _merged_envelope(ids["task0"], ids["project"]))
    assert rows and rows[0].status is AutomationExecutionStatus.SUCCEEDED
