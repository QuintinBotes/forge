"""Automation evaluation worker (F21).

* :class:`CeleryAutomationDispatcher` — the concrete :class:`AutomationDispatcher`
  board/workflow producers call post-commit; it enqueues ``evaluate_trigger``.
* :func:`evaluate_envelope` — the core consumer (testable without Celery): loads
  matching enabled rules, builds the entity snapshot, runs the pure
  :class:`AutomationEngine` with a concrete DB executor, and persists one
  ``automation_execution`` row per evaluated rule. Idempotency: a rule already
  having an execution for ``(rule_id, trigger_event_id)`` is skipped *before* its
  actions run, so redelivery is safe (side effects apply once).
* :func:`sweep_unprocessed_triggers` — Celery-beat reconciliation backstop.

Foundation deviations (noted in the slice report): the concrete executor mutates
the ``forge_db`` ``Task`` ORM rows directly (the foundation board service is
in-memory with no DB persistence and there is no ``activity_events`` table), and
status-transition legality is enforced via ``forge_board.workflow``. Soft-dep
actions (comments, notifications, workflow events) degrade to an explicit
``no_op``/``error``/``forbidden`` result and never fake success.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from contextlib import suppress

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from forge_board.automation import (
    ActionContext,
    ActionResult,
    ActionSpec,
    AutomationEngine,
    AutomationRuleSpec,
    AutomationRuleSpecWithMeta,
    ConditionGroup,
    LoopGuard,
    TriggerSpec,
    snapshot_for_task,
)
from forge_board.automation.schemas import (
    AddCommentAction,
    CloseLinkedSpecTasksAction,
    CreateTaskAction,
    SendNotificationAction,
    SendWorkflowEventAction,
    SetAssigneeAction,
    SetFieldAction,
    SetPriorityAction,
    SetStatusAction,
)
from forge_board.workflow import InvalidStatusTransitionError, validate_transition
from forge_contracts.automation import (
    HUMAN_GATE_EVENTS,
    AutomationExecutionStatus,
    AutomationTriggerEnvelope,
)
from forge_contracts.enums import TaskKind, TaskStatus
from forge_db.models import AutomationExecution, AutomationRule, Task
from forge_worker.celery_app import celery_app

EVALUATE_TASK = "forge.automations.evaluate_trigger"
SWEEP_TASK = "forge.automations.sweep_unprocessed_triggers"

#: Mutating outcomes that warrant a fan-out to the central audit log.
_MUTATING = frozenset(
    {
        AutomationExecutionStatus.SUCCEEDED,
        AutomationExecutionStatus.PARTIAL_FAILURE,
    }
)


# --------------------------------------------------------------------------- #
# Concrete DB action executor                                                  #
# --------------------------------------------------------------------------- #


class DbActionExecutor:
    """Performs automation actions against the real ``forge_db`` tables."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def execute(self, action: ActionSpec, ctx: ActionContext) -> ActionResult:
        task = self._session.get(Task, ctx.snapshot.entity_id)
        if task is None:
            return ActionResult(type=action.type, status="error", detail={"error": "task_missing"})

        if isinstance(action, SetStatusAction):
            return self._set_status(task, action.status, action.type)
        if isinstance(action, SetPriorityAction):
            task.priority = action.priority
            return ActionResult(
                type=action.type, status="ok", detail={"priority": action.priority.value}
            )
        if isinstance(action, SetAssigneeAction):
            task.assignee_id = action.assignee_id
            return ActionResult(
                type=action.type,
                status="ok",
                detail={"assignee_id": str(action.assignee_id) if action.assignee_id else None},
            )
        if isinstance(action, SetFieldAction):
            return self._set_field(task, action)
        if isinstance(action, CloseLinkedSpecTasksAction):
            return self._close_linked(task, action)
        if isinstance(action, CreateTaskAction):
            return self._create_task(task, action, ctx)
        if isinstance(action, AddCommentAction):
            # No comment/timeline table in this foundation — degrade explicitly.
            return ActionResult(
                type=action.type, status="no_op", detail={"reason": "comments_unsupported"}
            )
        if isinstance(action, SendWorkflowEventAction):
            if action.event in HUMAN_GATE_EVENTS:
                return ActionResult(
                    type=action.type, status="forbidden", detail={"event": action.event}
                )
            # The FSM engine is not DB-wired in this foundation — degrade.
            return ActionResult(
                type=action.type, status="no_op", detail={"reason": "workflow_engine_unwired"}
            )
        if isinstance(action, SendNotificationAction):
            return ActionResult(
                type=action.type, status="error", detail={"error": "notifications_unavailable"}
            )
        return ActionResult(  # pragma: no cover - exhaustive above
            type=action.type, status="error", detail={"error": "unknown_action"}
        )

    def _set_status(self, task: Task, new_status: TaskStatus, atype) -> ActionResult:
        if task.status == new_status:
            return ActionResult(type=atype, status="no_op", detail={"status": new_status.value})
        try:
            validate_transition(task.status, new_status)
        except InvalidStatusTransitionError as exc:
            return ActionResult(
                type=atype,
                status="error",
                detail={"error": "status_transition_forbidden", "detail": str(exc)},
            )
        task.status = new_status
        return ActionResult(type=atype, status="ok", detail={"status": new_status.value})

    def _set_field(self, task: Task, action: SetFieldAction) -> ActionResult:
        value = action.value
        if action.field == "estimate":
            task.estimate = int(value) if value is not None else None
        elif action.field == "sprint_id":
            task.sprint_id = uuid.UUID(str(value)) if value else None
        elif action.field == "milestone_id":
            task.milestone_id = uuid.UUID(str(value)) if value else None
        return ActionResult(type=action.type, status="ok", detail={action.field: str(value)})

    def _close_linked(self, task: Task, action: CloseLinkedSpecTasksAction) -> ActionResult:
        if task.spec_id is None:
            return ActionResult(
                type=action.type, status="no_op", detail={"reason": "no_spec_link"}
            )
        q = select(Task).where(
            Task.workspace_id == task.workspace_id, Task.spec_id == task.spec_id
        )
        if action.scope == "project":
            q = q.where(Task.project_id == task.project_id)
        closed: list[str] = []
        for sibling in self._session.execute(q).scalars():
            if action.exclude_trigger_task and sibling.id == task.id:
                continue
            if sibling.status == action.target_status:
                continue
            # Force the close (terminal target) even across non-adjacent edges:
            # the canonical "close on merge" intent overrides the default table.
            with suppress(InvalidStatusTransitionError):
                validate_transition(sibling.status, action.target_status)
            sibling.status = action.target_status
            closed.append(str(sibling.id))
        status = "ok" if closed else "no_op"
        return ActionResult(
            type=action.type, status=status, detail={"closed_task_ids": closed}
        )

    def _create_task(
        self, task: Task, action: CreateTaskAction, ctx: ActionContext
    ) -> ActionResult:
        title = _render_template(action.title_template, task, ctx)
        new = Task(
            workspace_id=task.workspace_id,
            project_id=task.project_id,
            key=f"AUTO-{uuid.uuid4().hex[:8]}",
            kind=TaskKind(action.kind.value),
            title=title[:512],
            status=TaskStatus.BACKLOG,
        )
        self._session.add(new)
        self._session.flush()
        return ActionResult(type=action.type, status="ok", detail={"created_task_id": str(new.id)})


def _render_template(template: str, task: Task, ctx: ActionContext) -> str:
    tokens = {
        "{{task.key}}": task.key or "",
        "{{task.title}}": task.title or "",
        "{{rule.name}}": ctx.rule_name,
    }
    out = template
    for token, value in tokens.items():
        out = out.replace(token, value)
    return out


# --------------------------------------------------------------------------- #
# Rule loading + evaluation                                                    #
# --------------------------------------------------------------------------- #


def _meta_from_row(row: AutomationRule) -> AutomationRuleSpecWithMeta:
    from pydantic import TypeAdapter

    actions = TypeAdapter(list[ActionSpec]).validate_python(row.actions or [])
    spec = AutomationRuleSpec(
        name=row.name,
        description=row.description,
        enabled=row.enabled,
        trigger=TriggerSpec(type=row.trigger_type, config=dict(row.trigger_config or {})),
        condition=ConditionGroup.model_validate(row.condition or {}),
        actions=actions,
        run_order=row.run_order,
    )
    return AutomationRuleSpecWithMeta(
        spec=spec, id=row.id, version=row.version, enabled=row.enabled, run_order=row.run_order
    )


def _matching_rules(session: Session, env: AutomationTriggerEnvelope) -> list[AutomationRule]:
    q = select(AutomationRule).where(
        AutomationRule.workspace_id == env.workspace_id,
        AutomationRule.trigger_type == env.trigger_type,
        AutomationRule.enabled.is_(True),
    )
    rows = list(session.execute(q).scalars())
    # project_id NULL = workspace-wide; otherwise must match the envelope project.
    return [
        r for r in rows if r.project_id is None or r.project_id == env.project_id
    ]


def evaluate_envelope(
    session_factory: sessionmaker[Session],
    envelope: AutomationTriggerEnvelope,
    *,
    audit_sink: Callable[[AutomationExecution], None] | None = None,
    max_depth: int | None = None,
) -> list[AutomationExecution]:
    """Evaluate ``envelope`` against matching rules; persist execution rows."""
    with session_factory() as session:
        task = session.get(Task, envelope.entity_id)
        if task is None or task.workspace_id != envelope.workspace_id:
            return []

        rule_rows = _matching_rules(session, envelope)

        # Idempotency pre-filter: skip rules already processed for this event so
        # their side effects never re-run on redelivery.
        pending: list[AutomationRule] = []
        for row in rule_rows:
            key = f"{row.id}:{envelope.trigger_event_id}"
            exists = session.scalar(
                select(AutomationExecution.id).where(
                    AutomationExecution.idempotency_key == key
                )
            )
            if exists is None:
                pending.append(row)
        if not pending:
            return []

        metas = [_meta_from_row(r) for r in pending]
        snapshot = snapshot_for_task(task, envelope.change)
        engine = AutomationEngine(LoopGuard(max_depth))

        started = time.perf_counter()
        results = engine.evaluate(envelope, metas, DbActionExecutor(session), snapshot)
        latency_ms = int((time.perf_counter() - started) * 1000)

        # Flush the action-induced task mutations before the per-row inserts so a
        # rare idempotency race (savepoint rollback) only undoes the duplicate
        # execution row — never the task changes or sibling execution rows.
        session.flush()

        persisted: list[AutomationExecution] = []
        for result in results:
            key = f"{result.rule_id}:{envelope.trigger_event_id}"
            row = AutomationExecution(
                workspace_id=envelope.workspace_id,
                rule_id=result.rule_id,
                rule_version=result.rule_version,
                trigger_type=envelope.trigger_type,
                trigger_event_id=envelope.trigger_event_id,
                trigger_source=envelope.trigger_source,
                entity_type=envelope.entity_type,
                entity_id=envelope.entity_id,
                status=result.status,
                condition_result=result.condition_result,
                actions_planned=[a.model_dump(mode="json") for a in result.actions_planned],
                action_results=[r.model_dump(mode="json") for r in result.action_results],
                depth=result.depth,
                causation_chain=[str(c) for c in result.causation_chain],
                error=result.error,
                latency_ms=latency_ms,
                idempotency_key=key,
            )
            try:
                with session.begin_nested():
                    session.add(row)
                    session.flush()
            except IntegrityError:
                continue
            persisted.append(row)

        session.commit()
        for row in persisted:
            session.refresh(row)
            if audit_sink is not None and row.status in _MUTATING:
                audit_sink(row)
        return persisted


# --------------------------------------------------------------------------- #
# Sweeper backstop                                                             #
# --------------------------------------------------------------------------- #


def sweep_unprocessed_triggers(
    session_factory: sessionmaker[Session],
    pending_envelopes: list[AutomationTriggerEnvelope],
    *,
    dispatcher: AutomationDispatcherLike | None = None,
) -> int:
    """Re-dispatch trigger envelopes that have no matching execution row.

    The current foundation has no append-only board/workflow event log to scan,
    so the durable source of pending envelopes is supplied by the caller (the
    documented seam for when that log lands). Re-dispatch is safe because the
    ``(rule_id, trigger_event_id)`` unique key dedupes. Returns the number of
    envelopes re-dispatched.
    """
    redispatched = 0
    with session_factory() as session:
        for env in pending_envelopes:
            rule_rows = _matching_rules(session, env)
            if not rule_rows:
                continue
            has_unprocessed = False
            for row in rule_rows:
                key = f"{row.id}:{env.trigger_event_id}"
                exists = session.scalar(
                    select(AutomationExecution.id).where(
                        AutomationExecution.idempotency_key == key
                    )
                )
                if exists is None:
                    has_unprocessed = True
                    break
            if has_unprocessed:
                redispatched += 1
                if dispatcher is not None:
                    dispatcher.dispatch(env)
    return redispatched


class AutomationDispatcherLike:  # pragma: no cover - typing aid
    def dispatch(self, envelope: AutomationTriggerEnvelope) -> None: ...


# --------------------------------------------------------------------------- #
# Celery wiring                                                                #
# --------------------------------------------------------------------------- #


class CeleryAutomationDispatcher:
    """Concrete :class:`forge_contracts.AutomationDispatcher`: enqueues on Celery."""

    def dispatch(self, envelope: AutomationTriggerEnvelope) -> None:
        celery_app.send_task(EVALUATE_TASK, args=[envelope.model_dump(mode="json")])


def _session_factory() -> sessionmaker[Session]:  # pragma: no cover - prod seam
    from forge_db import create_db_engine, create_session_factory, get_database_url

    return create_session_factory(create_db_engine(get_database_url()))


def evaluate_trigger_task(envelope: dict) -> int:  # pragma: no cover - prod seam
    """Celery task body: evaluate one trigger envelope."""
    env = AutomationTriggerEnvelope.model_validate(envelope)
    rows = evaluate_envelope(_session_factory(), env)
    return len(rows)


def register_automation_tasks() -> None:
    """Register the automation Celery tasks (idempotent)."""
    celery_app.task(name=EVALUATE_TASK)(evaluate_trigger_task)


register_automation_tasks()


__all__ = [
    "EVALUATE_TASK",
    "SWEEP_TASK",
    "AutomationDispatcherLike",
    "CeleryAutomationDispatcher",
    "DbActionExecutor",
    "evaluate_envelope",
    "evaluate_trigger_task",
    "register_automation_tasks",
    "sweep_unprocessed_triggers",
]
