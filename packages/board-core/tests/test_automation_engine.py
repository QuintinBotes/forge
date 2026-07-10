"""Unit tests for the pure automation engine (F21).

Hermetic: no DB, uses ``RecordingActionExecutor`` and hand-built envelopes.
Covers conditions, triggers, validators, loop guard, and engine orchestration
(ACs 2, 3, 5, 6, 8, 9, 10, 15, 19, 20).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from forge_board.automation import (
    ActionContext,
    ActionResult,
    AutomationEngine,
    AutomationRuleSpec,
    AutomationRuleSpecWithMeta,
    CloseLinkedSpecTasksAction,
    Condition,
    ConditionGroup,
    EntitySnapshot,
    LoopGuard,
    RecordingActionExecutor,
    RuleValidationError,
    SendWorkflowEventAction,
    SetPriorityAction,
    SetStatusAction,
    TriggerSpec,
    evaluate_condition,
    snapshot_for_task,
    trigger_matches,
    trigger_type_for,
    validate_rule,
)
from forge_board.automation.errors import ActionForbiddenError
from forge_contracts.automation import (
    AutomationEntityType,
    AutomationExecutionStatus,
    AutomationTriggerEnvelope,
    AutomationTriggerSource,
    AutomationTriggerType,
    ConditionOp,
)
from forge_contracts.enums import Priority, TaskStatus

# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #


def _snapshot(**fields: object) -> EntitySnapshot:
    base: dict[str, object] = {
        "status": TaskStatus.IN_REVIEW.value,
        "priority": Priority.MEDIUM.value,
        "spec_id": None,
    }
    change = fields.pop("change", {})
    base.update(fields)
    return EntitySnapshot(
        entity_type=AutomationEntityType.TASK,
        entity_id=uuid.uuid4(),
        fields=base,
        change=change,  # type: ignore[arg-type]
    )


def _envelope(
    trigger_type: AutomationTriggerType = AutomationTriggerType.WORKFLOW_STATE_CHANGED,
    *,
    change: dict | None = None,
    depth: int = 0,
    causation: list[uuid.UUID] | None = None,
    entity_id: uuid.UUID | None = None,
) -> AutomationTriggerEnvelope:
    return AutomationTriggerEnvelope(
        trigger_type=trigger_type,
        trigger_source=AutomationTriggerSource.WORKFLOW_TRANSITION,
        trigger_event_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        entity_type=AutomationEntityType.TASK,
        entity_id=entity_id or uuid.uuid4(),
        change=change or {},
        depth=depth,
        causation_chain=causation or [],
    )


def _meta(spec: AutomationRuleSpec, **kw: object) -> AutomationRuleSpecWithMeta:
    return AutomationRuleSpecWithMeta(
        spec=spec,
        id=kw.get("id", uuid.uuid4()),  # type: ignore[arg-type]
        version=kw.get("version", 1),  # type: ignore[arg-type]
        enabled=kw.get("enabled", spec.enabled),  # type: ignore[arg-type]
        run_order=kw.get("run_order", spec.run_order),  # type: ignore[arg-type]
    )


def _close_spec_rule(**spec_kw: object) -> AutomationRuleSpec:
    return AutomationRuleSpec(
        name="Close spec tasks on merge",
        trigger=TriggerSpec(
            type=AutomationTriggerType.WORKFLOW_STATE_CHANGED, config={"to_state": "merged"}
        ),
        actions=[CloseLinkedSpecTasksAction()],
        **spec_kw,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# conditions                                                                   #
# --------------------------------------------------------------------------- #


def test_condition_eq_and_in() -> None:
    snap = _snapshot(priority=Priority.URGENT.value)
    g = ConditionGroup(conditions=[Condition(field="priority", op=ConditionOp.EQ, value="urgent")])
    assert evaluate_condition(g, snap) is True

    g2 = ConditionGroup(
        conditions=[Condition(field="priority", op=ConditionOp.IN, value=["low", "medium"])]
    )
    assert evaluate_condition(g2, snap) is False


def test_condition_has_spec_and_is_null() -> None:
    with_spec = _snapshot(spec_id=str(uuid.uuid4()))
    without = _snapshot(spec_id=None)
    g = ConditionGroup(conditions=[Condition(field="has_spec", op=ConditionOp.EQ, value=True)])
    assert evaluate_condition(g, with_spec) is True
    assert evaluate_condition(g, without) is False

    g_null = ConditionGroup(conditions=[Condition(field="spec_id", op=ConditionOp.IS_NULL)])
    assert evaluate_condition(g_null, without) is True


def test_condition_changed_op_uses_change() -> None:
    snap = _snapshot(change={"to_status": "done"})
    g = ConditionGroup(conditions=[Condition(field="to_status", op=ConditionOp.CHANGED)])
    assert evaluate_condition(g, snap) is True
    assert (
        evaluate_condition(
            ConditionGroup(conditions=[Condition(field="from_status", op=ConditionOp.CHANGED)]),
            snap,
        )
        is False
    )


def test_condition_all_any_nesting() -> None:
    snap = _snapshot(priority=Priority.URGENT.value, spec_id=str(uuid.uuid4()))
    g = ConditionGroup(
        match="any",
        conditions=[Condition(field="priority", op=ConditionOp.EQ, value="low")],
        groups=[
            ConditionGroup(
                match="all",
                conditions=[
                    Condition(field="priority", op=ConditionOp.EQ, value="urgent"),
                    Condition(field="has_spec", op=ConditionOp.EQ, value=True),
                ],
            )
        ],
    )
    assert evaluate_condition(g, snap) is True


def test_condition_empty_group_is_true() -> None:
    assert evaluate_condition(ConditionGroup(), _snapshot()) is True


def test_conditions_use_shared_contracts_primitive() -> None:
    """F40 consolidation: the engine rides the ONE shared condition DSL.

    ``Condition``/``ConditionGroup``/``ConditionOp`` are the identical objects the
    policy engine uses — no drifted F21-private copies remain.
    """
    from forge_contracts import conditions as shared

    assert Condition is shared.Condition
    assert ConditionGroup is shared.ConditionGroup
    assert ConditionOp is shared.ConditionOp


def test_condition_aggregate_all_subtasks_done() -> None:
    """F40 aggregate condition: ``all_subtasks_done`` derived over subtasks."""
    done = SimpleNamespace(status=TaskStatus.DONE.value)
    open_ = SimpleNamespace(status=TaskStatus.IN_PROGRESS.value)

    parent_all_done = SimpleNamespace(
        id=uuid.uuid4(), status=TaskStatus.IN_REVIEW.value, subtasks=[done, done]
    )
    parent_open = SimpleNamespace(
        id=uuid.uuid4(), status=TaskStatus.IN_REVIEW.value, subtasks=[done, open_]
    )

    g = ConditionGroup(
        conditions=[Condition(field="all_subtasks_done", op=ConditionOp.EQ, value=True)]
    )
    assert evaluate_condition(g, snapshot_for_task(parent_all_done)) is True
    assert evaluate_condition(g, snapshot_for_task(parent_open)) is False

    # The numeric aggregates are exposed too.
    open_count = ConditionGroup(
        conditions=[Condition(field="open_subtask_count", op=ConditionOp.GTE, value=1)]
    )
    assert evaluate_condition(open_count, snapshot_for_task(parent_open)) is True
    assert evaluate_condition(open_count, snapshot_for_task(parent_all_done)) is False


def test_condition_aggregate_from_explicit_subtask_statuses() -> None:
    """F40 aggregate condition off explicit child *statuses* — the real DB path.

    The DB-backed worker has no ``.subtasks`` attribute on the ``Task`` ORM row;
    it resolves the children from the ``task_dependency`` graph and passes their
    status values via ``subtasks=``. This exercises exactly that shape.
    """
    parent = SimpleNamespace(id=uuid.uuid4(), status=TaskStatus.IN_REVIEW.value)
    all_done = snapshot_for_task(
        parent, subtasks=[TaskStatus.DONE.value, TaskStatus.CANCELLED.value]
    )
    one_open = snapshot_for_task(
        parent, subtasks=[TaskStatus.DONE.value, TaskStatus.IN_PROGRESS.value]
    )
    none = snapshot_for_task(parent, subtasks=[])

    done_gate = ConditionGroup(
        conditions=[Condition(field="all_subtasks_done", op=ConditionOp.EQ, value=True)]
    )
    assert evaluate_condition(done_gate, all_done) is True
    assert evaluate_condition(done_gate, one_open) is False
    # A childless task is vacuously "all done" (nothing open).
    assert evaluate_condition(done_gate, none) is True

    count_gate = ConditionGroup(
        conditions=[Condition(field="subtask_count", op=ConditionOp.EQ, value=2)]
    )
    assert evaluate_condition(count_gate, all_done) is True
    assert (
        evaluate_condition(
            ConditionGroup(
                conditions=[Condition(field="open_subtask_count", op=ConditionOp.EQ, value=1)]
            ),
            one_open,
        )
        is True
    )


def test_condition_unknown_field_raises() -> None:
    g = ConditionGroup(conditions=[Condition(field="nope", op=ConditionOp.EQ, value=1)])
    with pytest.raises(ValueError):
        evaluate_condition(g, _snapshot())


def test_condition_in_without_list_raises() -> None:
    g = ConditionGroup(conditions=[Condition(field="priority", op=ConditionOp.IN, value="x")])
    with pytest.raises(ValueError):
        evaluate_condition(g, _snapshot())


# --------------------------------------------------------------------------- #
# triggers                                                                     #
# --------------------------------------------------------------------------- #


def test_trigger_type_for_workflow_and_board() -> None:
    assert (
        trigger_type_for(source=AutomationTriggerSource.WORKFLOW_TRANSITION, event_type="anything")
        is AutomationTriggerType.WORKFLOW_STATE_CHANGED
    )
    assert (
        trigger_type_for(
            source=AutomationTriggerSource.BOARD_ACTIVITY, event_type="priority_changed"
        )
        is AutomationTriggerType.TASK_PRIORITY_CHANGED
    )


def test_trigger_matches_to_state_config() -> None:
    spec = TriggerSpec(
        type=AutomationTriggerType.WORKFLOW_STATE_CHANGED, config={"to_state": "merged"}
    )
    assert trigger_matches(spec, _envelope(change={"to_state": "merged"})) is True
    assert trigger_matches(spec, _envelope(change={"to_state": "verifying"})) is False


# --------------------------------------------------------------------------- #
# validators                                                                   #
# --------------------------------------------------------------------------- #


def test_validate_missing_trigger_config() -> None:
    spec = AutomationRuleSpec(
        name="x",
        trigger=TriggerSpec(type=AutomationTriggerType.WORKFLOW_STATE_CHANGED, config={}),
        actions=[SetStatusAction(status=TaskStatus.DONE)],
    )
    with pytest.raises(RuleValidationError):
        validate_rule(spec)


def test_validate_human_gate_event_forbidden() -> None:
    spec = AutomationRuleSpec(
        name="x",
        trigger=TriggerSpec(
            type=AutomationTriggerType.WORKFLOW_STATE_CHANGED, config={"to_state": "merged"}
        ),
        actions=[SendWorkflowEventAction(event="review_approved_by_human")],
    )
    with pytest.raises(ActionForbiddenError):
        validate_rule(spec)


def test_validate_non_gate_event_ok() -> None:
    spec = AutomationRuleSpec(
        name="x",
        trigger=TriggerSpec(
            type=AutomationTriggerType.WORKFLOW_STATE_CHANGED, config={"to_state": "merged"}
        ),
        actions=[SendWorkflowEventAction(event="close")],
    )
    assert validate_rule(spec) == []


def test_validate_unknown_condition_field() -> None:
    spec = AutomationRuleSpec(
        name="x",
        trigger=TriggerSpec(
            type=AutomationTriggerType.WORKFLOW_STATE_CHANGED, config={"to_state": "merged"}
        ),
        condition=ConditionGroup(conditions=[Condition(field="bogus", op=ConditionOp.EQ, value=1)]),
        actions=[SetStatusAction(status=TaskStatus.DONE)],
    )
    with pytest.raises(RuleValidationError):
        validate_rule(spec)


def test_validate_malformed_cron_rejected() -> None:
    spec = AutomationRuleSpec(
        name="x",
        trigger=TriggerSpec(type=AutomationTriggerType.SCHEDULED, config={"cron": "not a cron"}),
        actions=[SetPriorityAction(priority=Priority.HIGH)],
    )
    with pytest.raises(RuleValidationError):
        validate_rule(spec)


def test_validate_well_formed_cron_ok() -> None:
    spec = AutomationRuleSpec(
        name="x",
        trigger=TriggerSpec(
            type=AutomationTriggerType.SCHEDULED, config={"cron": "*/15 9 * * 1-5"}
        ),
        actions=[SetPriorityAction(priority=Priority.HIGH)],
    )
    assert validate_rule(spec) == []


def test_validate_possible_self_trigger_warning() -> None:
    spec = AutomationRuleSpec(
        name="x",
        trigger=TriggerSpec(
            type=AutomationTriggerType.WORKFLOW_STATE_CHANGED, config={"to_state": "merged"}
        ),
        actions=[SetStatusAction(status=TaskStatus.DONE)],
    )
    warnings = validate_rule(spec)
    assert any(w.code == "possible_self_trigger" for w in warnings)


# --------------------------------------------------------------------------- #
# loop guard                                                                   #
# --------------------------------------------------------------------------- #


def test_loop_guard_depth_cutoff() -> None:
    guard = LoopGuard(max_depth=3)
    rid = uuid.uuid4()
    assert guard.abort_reason(_envelope(depth=2), rid) is None
    assert guard.abort_reason(_envelope(depth=3), rid) is not None


def test_loop_guard_self_cycle() -> None:
    guard = LoopGuard(max_depth=10)
    rid = uuid.uuid4()
    assert guard.abort_reason(_envelope(depth=0, causation=[rid]), rid) == "self_cycle"


# --------------------------------------------------------------------------- #
# engine                                                                       #
# --------------------------------------------------------------------------- #


def test_engine_condition_fail_no_executor_calls() -> None:
    rule = _meta(
        _close_spec_rule(
            condition=ConditionGroup(
                conditions=[Condition(field="has_spec", op=ConditionOp.EQ, value=True)]
            )
        )
    )
    engine = AutomationEngine()
    executor = RecordingActionExecutor()
    results = engine.evaluate(
        _envelope(change={"to_state": "merged"}), [rule], executor, _snapshot(spec_id=None)
    )
    assert len(results) == 1
    assert results[0].status is AutomationExecutionStatus.CONDITIONS_FAILED
    assert executor.planned == []


def test_engine_happy_path_executes_actions() -> None:
    rule = _meta(_close_spec_rule())
    engine = AutomationEngine()
    executor = RecordingActionExecutor()
    results = engine.evaluate(
        _envelope(change={"to_state": "merged"}),
        [rule],
        executor,
        _snapshot(spec_id=str(uuid.uuid4())),
    )
    assert results[0].status is AutomationExecutionStatus.SUCCEEDED
    assert len(executor.planned) == 1


def test_engine_disabled_rule_skipped() -> None:
    rule = _meta(_close_spec_rule(enabled=False), enabled=False)
    engine = AutomationEngine()
    results = engine.evaluate(
        _envelope(change={"to_state": "merged"}),
        [rule],
        RecordingActionExecutor(),
        _snapshot(spec_id=str(uuid.uuid4())),
    )
    assert results[0].status is AutomationExecutionStatus.SKIPPED_DISABLED


def test_engine_non_matching_trigger_returns_no_result() -> None:
    rule = _meta(_close_spec_rule())
    engine = AutomationEngine()
    results = engine.evaluate(
        _envelope(change={"to_state": "verifying"}),
        [rule],
        RecordingActionExecutor(),
        _snapshot(spec_id=str(uuid.uuid4())),
    )
    assert results == []


def test_engine_orders_by_run_order() -> None:
    fired: list[int] = []

    class OrderExecutor:
        def execute(self, action, ctx):
            fired.append(ctx.depth)
            return ActionResult(type=action.type, status="ok")

    r1 = _meta(_close_spec_rule(run_order=200), run_order=200)
    r2 = _meta(_close_spec_rule(run_order=50), run_order=50)
    engine = AutomationEngine()
    results = engine.evaluate(
        _envelope(change={"to_state": "merged"}),
        [r1, r2],
        OrderExecutor(),
        _snapshot(spec_id=str(uuid.uuid4())),
    )
    # Both fire; lower run_order first.
    assert [res.rule_id for res in results] == [r2.id, r1.id]


def test_engine_partial_failure() -> None:
    class FlakyExecutor:
        def __init__(self) -> None:
            self.n = 0

        def execute(self, action, ctx: ActionContext) -> ActionResult:
            self.n += 1
            if self.n == 1:
                return ActionResult(type=action.type, status="ok")
            return ActionResult(type=action.type, status="error", detail={"error": "blocked"})

    spec = AutomationRuleSpec(
        name="multi",
        trigger=TriggerSpec(
            type=AutomationTriggerType.WORKFLOW_STATE_CHANGED, config={"to_state": "merged"}
        ),
        actions=[SetStatusAction(status=TaskStatus.DONE), CloseLinkedSpecTasksAction()],
    )
    results = AutomationEngine().evaluate(
        _envelope(change={"to_state": "merged"}),
        [_meta(spec)],
        FlakyExecutor(),
        _snapshot(spec_id=str(uuid.uuid4())),
    )
    assert results[0].status is AutomationExecutionStatus.PARTIAL_FAILURE


def test_engine_loop_skip_records_status() -> None:
    rule = _meta(_close_spec_rule())
    engine = AutomationEngine(LoopGuard(max_depth=2))
    results = engine.evaluate(
        _envelope(change={"to_state": "merged"}, depth=2),
        [rule],
        RecordingActionExecutor(),
        _snapshot(spec_id=str(uuid.uuid4())),
    )
    assert results[0].status is AutomationExecutionStatus.SKIPPED_LOOP
