"""Persistence + orchestration for the automations router (F21).

Owns ``automation_rule`` CRUD (with validation via the engine's
``validate_rule``), the side-effect-free dry-run (builds an ``EntitySnapshot``
from a real task and evaluates with a ``RecordingActionExecutor``), and
execution-history reads. Every authoring mutation fans out an immutable
``AuditEvent`` through the shared :class:`AuditLog`.

All reads/writes are scoped by ``workspace_id`` (tenant isolation; a foreign id
is 404, no existence leak).
"""

from __future__ import annotations

import builtins
import os
import uuid
from datetime import datetime
from functools import lru_cache

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from forge_api.observability.audit import AuditCategory, AuditLog
from forge_api.schemas.automations import (
    AutomationExecutionRead,
    AutomationRuleCreate,
    AutomationRuleRead,
    AutomationRuleUpdate,
    DryRunResult,
    RuleWarningModel,
)
from forge_board.automation import (
    ActionSpec,
    AutomationEngine,
    AutomationRuleSpec,
    AutomationRuleSpecWithMeta,
    ConditionGroup,
    RecordingActionExecutor,
    RuleRefContext,
    TriggerSpec,
    snapshot_for_task,
    trigger_matches,
    validate_rule,
)
from forge_contracts.automation import (
    AutomationTriggerEnvelope,
    AutomationTriggerSource,
)
from forge_db.models import AutomationExecution, AutomationRule, Milestone, Sprint, Task, User

_ACTIONS_ADAPTER: TypeAdapter[list[ActionSpec]] = TypeAdapter(list[ActionSpec])

_WORKFLOW_TRIGGERS = frozenset({"workflow_state_changed"})

#: Must match ``apps/worker/forge_worker/tasks/automations.py::EVALUATE_TASK`` —
#: duplicated (not imported) because ``forge-api`` cannot depend on
#: ``forge-worker`` (the dependency runs the other way); mirrors the existing
#: ``FULL_SYNC_TASK`` pattern in ``mcp_index_service.py``.
_EVALUATE_TRIGGER_TASK = "forge.automations.evaluate_trigger"


@lru_cache(maxsize=1)
def _celery_app() -> object:
    from celery import Celery

    url = os.environ.get("FORGE_REDIS_URL", "redis://localhost:6379/0")
    return Celery("forge-api-enqueue", broker=url, backend=url)


class ApiAutomationDispatcher:
    """Concrete :class:`forge_contracts.automation.AutomationDispatcher`.

    Enqueues onto the worker's ``evaluate_trigger`` Celery task by name (never
    imports ``forge_worker``) — the seam API-side producers (e.g. the sprint
    router's :class:`~forge_board.sprint_service.SprintService`) dispatch
    through so a fired ``AutomationTriggerEnvelope`` actually reaches the real
    ``evaluate_envelope`` dispatch path.
    """

    def dispatch(self, envelope: AutomationTriggerEnvelope) -> None:
        _celery_app().send_task(  # type: ignore[attr-defined]
            _EVALUATE_TRIGGER_TASK, args=[envelope.model_dump(mode="json")]
        )


class RuleNotFound(LookupError):
    """A rule id is absent in the caller's workspace."""


class VersionConflict(ValueError):
    """An update's ``version`` is stale relative to the persisted row."""

    def __init__(self, current_version: int) -> None:
        self.current_version = current_version
        super().__init__(f"stale version (current={current_version})")


class AutomationRuleService:
    def __init__(
        self, *, session_factory: sessionmaker[Session], audit: AuditLog | None = None
    ) -> None:
        self._sf = session_factory
        self._audit = audit or AuditLog()

    # ------------------------------------------------------------------ #
    # helpers                                                            #
    # ------------------------------------------------------------------ #

    def _ref_context(
        self, session: Session, workspace_id: uuid.UUID, project_id: uuid.UUID | None
    ) -> RuleRefContext:
        users = session.execute(select(User.id).where(User.workspace_id == workspace_id)).scalars()
        sprint_q = select(Sprint.id).where(Sprint.workspace_id == workspace_id)
        milestone_q = select(Milestone.id).where(Milestone.workspace_id == workspace_id)
        if project_id is not None:
            sprint_q = sprint_q.where(Sprint.project_id == project_id)
            milestone_q = milestone_q.where(Milestone.project_id == project_id)
        return RuleRefContext(
            valid_assignee_ids=frozenset(users),
            valid_sprint_ids=frozenset(session.execute(sprint_q).scalars()),
            valid_milestone_ids=frozenset(session.execute(milestone_q).scalars()),
            check_references=True,
        )

    def _row(self, session: Session, workspace_id: uuid.UUID, rule_id: uuid.UUID) -> AutomationRule:
        row = session.get(AutomationRule, rule_id)
        if row is None or row.workspace_id != workspace_id:
            raise RuleNotFound(str(rule_id))
        return row

    @staticmethod
    def _spec_from_row(row: AutomationRule) -> AutomationRuleSpec:
        return AutomationRuleSpec(
            name=row.name,
            description=row.description,
            enabled=row.enabled,
            trigger=TriggerSpec(type=row.trigger_type, config=dict(row.trigger_config or {})),
            condition=ConditionGroup.model_validate(row.condition or {}),
            actions=_ACTIONS_ADAPTER.validate_python(row.actions or []),
            run_order=row.run_order,
        )

    def _read(
        self, row: AutomationRule, warnings: list[RuleWarningModel] | None = None
    ) -> AutomationRuleRead:
        spec = self._spec_from_row(row)
        return AutomationRuleRead(
            id=row.id,
            workspace_id=row.workspace_id,
            project_id=row.project_id,
            name=spec.name,
            description=spec.description,
            enabled=spec.enabled,
            trigger=spec.trigger,
            condition=spec.condition,
            actions=spec.actions,
            run_order=spec.run_order,
            version=row.version,
            created_by=row.created_by,
            created_at=row.created_at,
            updated_at=row.updated_at,
            warnings=warnings or [],
        )

    def _record_audit(
        self, action: str, *, workspace_id: uuid.UUID, actor: str, rule_id: uuid.UUID
    ) -> None:
        self._audit.record(
            category=AuditCategory.SYSTEM,
            action=action,
            actor=actor,
            workspace_id=workspace_id,
            target=f"automation_rule:{rule_id}",
        )

    # ------------------------------------------------------------------ #
    # CRUD                                                               #
    # ------------------------------------------------------------------ #

    def create(
        self,
        *,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        body: AutomationRuleCreate,
        actor_user_id: uuid.UUID,
    ) -> AutomationRuleRead:
        spec = AutomationRuleSpec.model_validate(body.model_dump())
        with self._sf() as session:
            warnings = validate_rule(spec, self._ref_context(session, workspace_id, project_id))
            row = AutomationRule(
                workspace_id=workspace_id,
                project_id=project_id,
                name=spec.name,
                description=spec.description,
                enabled=spec.enabled,
                trigger_type=spec.trigger.type,
                trigger_config=spec.trigger.config,
                condition=spec.condition.model_dump(),
                actions=[a.model_dump(mode="json") for a in spec.actions],
                run_order=spec.run_order,
                created_by=actor_user_id,
                version=1,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            self._record_audit(
                "automation.rule.created",
                workspace_id=workspace_id,
                actor=f"user:{actor_user_id}",
                rule_id=row.id,
            )
            models = [RuleWarningModel(code=w.code, message=w.message) for w in warnings]
            return self._read(row, models)

    def list(
        self, *, workspace_id: uuid.UUID, project_id: uuid.UUID | None = None
    ) -> list[AutomationRuleRead]:
        with self._sf() as session:
            q = select(AutomationRule).where(AutomationRule.workspace_id == workspace_id)
            if project_id is not None:
                q = q.where(AutomationRule.project_id == project_id)
            q = q.order_by(AutomationRule.run_order, AutomationRule.created_at)
            return [self._read(r) for r in session.execute(q).scalars()]

    def get(self, *, workspace_id: uuid.UUID, rule_id: uuid.UUID) -> AutomationRuleRead:
        with self._sf() as session:
            return self._read(self._row(session, workspace_id, rule_id))

    def update(
        self,
        *,
        workspace_id: uuid.UUID,
        rule_id: uuid.UUID,
        patch: AutomationRuleUpdate,
        actor_user_id: uuid.UUID,
    ) -> AutomationRuleRead:
        with self._sf() as session:
            row = self._row(session, workspace_id, rule_id)
            if patch.version != row.version:
                raise VersionConflict(row.version)

            spec = self._spec_from_row(row)
            data = spec.model_dump()
            if patch.name is not None:
                data["name"] = patch.name
            if patch.description is not None:
                data["description"] = patch.description
            if patch.enabled is not None:
                data["enabled"] = patch.enabled
            if patch.trigger is not None:
                data["trigger"] = patch.trigger.model_dump()
            if patch.condition is not None:
                data["condition"] = patch.condition.model_dump()
            if patch.actions is not None:
                data["actions"] = [a.model_dump() for a in patch.actions]
            if patch.run_order is not None:
                data["run_order"] = patch.run_order
            new_spec = AutomationRuleSpec.model_validate(data)
            warnings = validate_rule(
                new_spec, self._ref_context(session, workspace_id, row.project_id)
            )

            row.name = new_spec.name
            row.description = new_spec.description
            row.enabled = new_spec.enabled
            row.trigger_type = new_spec.trigger.type
            row.trigger_config = new_spec.trigger.config
            row.condition = new_spec.condition.model_dump()
            row.actions = [a.model_dump(mode="json") for a in new_spec.actions]
            row.run_order = new_spec.run_order
            row.version = row.version + 1
            session.commit()
            session.refresh(row)
            self._record_audit(
                "automation.rule.updated",
                workspace_id=workspace_id,
                actor=f"user:{actor_user_id}",
                rule_id=row.id,
            )
            models = [RuleWarningModel(code=w.code, message=w.message) for w in warnings]
            return self._read(row, models)

    def set_enabled(
        self,
        *,
        workspace_id: uuid.UUID,
        rule_id: uuid.UUID,
        enabled: bool,
        actor_user_id: uuid.UUID,
    ) -> AutomationRuleRead:
        with self._sf() as session:
            row = self._row(session, workspace_id, rule_id)
            row.enabled = enabled
            row.version = row.version + 1
            session.commit()
            session.refresh(row)
            self._record_audit(
                f"automation.rule.{'enabled' if enabled else 'disabled'}",
                workspace_id=workspace_id,
                actor=f"user:{actor_user_id}",
                rule_id=row.id,
            )
            return self._read(row)

    def delete(
        self, *, workspace_id: uuid.UUID, rule_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> None:
        with self._sf() as session:
            row = self._row(session, workspace_id, rule_id)
            session.delete(row)
            session.commit()
            self._record_audit(
                "automation.rule.deleted",
                workspace_id=workspace_id,
                actor=f"user:{actor_user_id}",
                rule_id=rule_id,
            )

    # ------------------------------------------------------------------ #
    # dry-run + history                                                  #
    # ------------------------------------------------------------------ #

    def dry_run(
        self,
        *,
        workspace_id: uuid.UUID,
        rule_id: uuid.UUID,
        task_id: uuid.UUID,
        change: dict,
    ) -> DryRunResult:
        with self._sf() as session:
            row = self._row(session, workspace_id, rule_id)
            spec = self._spec_from_row(row)
            task = session.get(Task, task_id)
            if task is None or task.workspace_id != workspace_id:
                raise RuleNotFound(str(task_id))

            snapshot = snapshot_for_task(task, change)
            source = (
                AutomationTriggerSource.WORKFLOW_TRANSITION
                if row.trigger_type.value in _WORKFLOW_TRIGGERS
                else AutomationTriggerSource.BOARD_ACTIVITY
            )
            envelope = AutomationTriggerEnvelope(
                trigger_type=row.trigger_type,
                trigger_source=source,
                trigger_event_id=uuid.uuid4(),
                workspace_id=workspace_id,
                project_id=row.project_id,
                entity_id=task_id,
                change=change,
            )
            matched = trigger_matches(spec.trigger, envelope)
            executor = RecordingActionExecutor()
            meta = AutomationRuleSpecWithMeta(
                spec=spec,
                id=row.id,
                version=row.version,
                enabled=row.enabled,
                run_order=row.run_order,
            )
            notes: list[str] = []
            results = AutomationEngine().evaluate(envelope, [meta], executor, snapshot)
            condition_result = bool(results and results[0].condition_result)
            if not matched:
                notes.append("trigger config does not match the supplied change")
            return DryRunResult(
                trigger_matched=matched,
                condition_result=condition_result,
                planned_actions=list(executor.planned),
                notes=notes,
            )

    def executions(
        self,
        *,
        workspace_id: uuid.UUID,
        rule_id: uuid.UUID,
        limit: int = 50,
        before: datetime | None = None,
    ) -> builtins.list[AutomationExecutionRead]:
        with self._sf() as session:
            self._row(session, workspace_id, rule_id)  # tenant check / 404
            q = select(AutomationExecution).where(
                AutomationExecution.workspace_id == workspace_id,
                AutomationExecution.rule_id == rule_id,
            )
            if before is not None:
                q = q.where(AutomationExecution.created_at < before)
            q = q.order_by(AutomationExecution.created_at.desc()).limit(limit)
            return [
                AutomationExecutionRead(
                    id=e.id,
                    rule_id=e.rule_id,
                    rule_version=e.rule_version,
                    trigger_type=e.trigger_type,
                    entity_type=e.entity_type,
                    entity_id=e.entity_id,
                    status=e.status,
                    condition_result=e.condition_result,
                    actions_planned=list(e.actions_planned or []),
                    action_results=list(e.action_results or []),
                    depth=e.depth,
                    causation_chain=list(e.causation_chain or []),
                    error=e.error,
                    latency_ms=e.latency_ms,
                    created_at=e.created_at,
                )
                for e in session.execute(q).scalars()
            ]


__all__ = [
    "ApiAutomationDispatcher",
    "AutomationRuleService",
    "RuleNotFound",
    "VersionConflict",
]
