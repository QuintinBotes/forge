"""Regression coverage for the F40-AUT-ACTIONS repair.

The real Celery worker dispatch path (``evaluate_envelope``) — not a hand-built
test double — must actually route the six F40 action types through
``forge_api.services.automation_actions.ExternalActionExecutor``. Prior to this
wiring, ``evaluate_envelope`` only ever constructed ``DbActionExecutor``, whose
isinstance chain has no branch for the new action types: every one of them fell
through to ``ActionResult(status="error", detail={"error": "unknown_action"})``
and no webhook posted / incident declared / sprint started / merge attempted.

Hermetic: in-memory SQLite (StaticPool). The one real outbound call
(``WEBHOOK_POST``) is exercised against a monkeypatched ``httpx.post`` — no
network. Everything else (incident service, sprint service, merge gate) is the
real production port from ``automation_actions.py``, composed exactly as
``evaluate_envelope`` composes it in production.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

from forge_contracts.automation import (
    AutomationEntityType,
    AutomationExecutionStatus,
    AutomationTriggerEnvelope,
    AutomationTriggerSource,
    AutomationTriggerType,
)
from forge_contracts.enums import IncidentSeverity
from forge_db.base import Base
from forge_db.models import AutomationRule, Project, Sprint, Task, Workspace
from forge_db.models.enums import SprintState, TaskStatus
from forge_worker.tasks.automations import evaluate_envelope

WS_ID = uuid.uuid4()


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def _seed_task(factory: sessionmaker[Session]) -> tuple[uuid.UUID, uuid.UUID]:
    with factory() as session:
        session.add(Workspace(id=WS_ID, name="Acme", slug="acme"))
        session.flush()
        project = Project(workspace_id=WS_ID, name="Core", key="CORE")
        session.add(project)
        session.flush()
        task = Task(
            workspace_id=WS_ID,
            project_id=project.id,
            key="CORE-1",
            title="Ship it",
            status=TaskStatus.IN_PROGRESS,
        )
        session.add(task)
        session.commit()
        return project.id, task.id


def _seed_planned_sprint(factory: sessionmaker[Session]) -> tuple[uuid.UUID, uuid.UUID]:
    with factory() as session:
        session.add(Workspace(id=WS_ID, name="Acme", slug="acme"))
        session.flush()
        project = Project(workspace_id=WS_ID, name="Core", key="CORE")
        session.add(project)
        session.flush()
        sprint = Sprint(workspace_id=WS_ID, project_id=project.id, name="Sprint 1")
        session.add(sprint)
        session.commit()
        return project.id, sprint.id


def _add_rule(
    factory: sessionmaker[Session],
    *,
    project_id: uuid.UUID,
    trigger_type: AutomationTriggerType,
    actions: list[dict[str, Any]],
) -> None:
    with factory() as session:
        session.add(
            AutomationRule(
                workspace_id=WS_ID,
                project_id=project_id,
                name=f"rule-{trigger_type.value}",
                enabled=True,
                trigger_type=trigger_type,
                trigger_config={},
                condition={},
                actions=actions,
                run_order=100,
            )
        )
        session.commit()


def _envelope(
    *,
    trigger_type: AutomationTriggerType,
    project_id: uuid.UUID,
    entity_id: uuid.UUID,
    entity_type: AutomationEntityType = AutomationEntityType.TASK,
    change: dict[str, Any] | None = None,
) -> AutomationTriggerEnvelope:
    return AutomationTriggerEnvelope(
        trigger_type=trigger_type,
        trigger_source=AutomationTriggerSource.BOARD_ACTIVITY,
        trigger_event_id=uuid.uuid4(),
        workspace_id=WS_ID,
        project_id=project_id,
        entity_type=entity_type,
        entity_id=entity_id,
        change=change or {},
    )


def test_sla_breach_declares_a_real_incident_through_the_worker_dispatch_path(
    factory: sessionmaker[Session],
) -> None:
    project_id, task_id = _seed_task(factory)
    _add_rule(
        factory,
        project_id=project_id,
        trigger_type=AutomationTriggerType.TASK_SLA_BREACHED,
        actions=[
            {
                "type": "declare_incident",
                "severity": IncidentSeverity.HIGH.value,
                "title_template": "SLA breach: {{entity.id}}",
            }
        ],
    )
    envelope = _envelope(
        trigger_type=AutomationTriggerType.TASK_SLA_BREACHED,
        project_id=project_id,
        entity_id=task_id,
    )

    executions = evaluate_envelope(factory, envelope)

    assert len(executions) == 1
    execution = executions[0]
    assert execution.status == AutomationExecutionStatus.SUCCEEDED
    assert len(execution.action_results) == 1
    result = execution.action_results[0]
    assert result["status"] == "ok"
    assert result["detail"]["incident_key"].startswith("INC-")


def test_auto_merge_is_forbidden_by_default_through_the_worker_dispatch_path(
    factory: sessionmaker[Session],
) -> None:
    project_id, task_id = _seed_task(factory)
    _add_rule(
        factory,
        project_id=project_id,
        trigger_type=AutomationTriggerType.PR_MERGED,
        actions=[{"type": "auto_merge", "enabled": False, "merge_method": "squash"}],
    )
    envelope = _envelope(
        trigger_type=AutomationTriggerType.PR_MERGED, project_id=project_id, entity_id=task_id
    )

    executions = evaluate_envelope(factory, envelope)

    assert len(executions) == 1
    result = executions[0].action_results[0]
    assert result["status"] == "forbidden"
    assert result["detail"]["reason"] == "auto_merge_disabled"


def test_webhook_post_makes_a_real_http_call_through_the_worker_dispatch_path(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, task_id = _seed_task(factory)
    _add_rule(
        factory,
        project_id=project_id,
        trigger_type=AutomationTriggerType.TASK_STATUS_CHANGED,
        actions=[
            {
                "type": "webhook_post",
                "url": "https://hooks.example.test/ci",
                "payload_template": {"event": "status_changed"},
            }
        ],
    )
    envelope = _envelope(
        trigger_type=AutomationTriggerType.TASK_STATUS_CHANGED,
        project_id=project_id,
        entity_id=task_id,
        change={"to_status": "in_review"},
    )

    calls: list[tuple[str, dict[str, Any]]] = []

    class _FakeResponse:
        status_code = 204

    def _fake_post(url: str, *, json: dict[str, Any], timeout: float) -> _FakeResponse:
        calls.append((url, json))
        return _FakeResponse()

    monkeypatch.setattr(httpx, "post", _fake_post)

    executions = evaluate_envelope(factory, envelope)

    assert len(executions) == 1
    result = executions[0].action_results[0]
    assert result["status"] == "ok"
    assert len(calls) == 1
    url, payload = calls[0]
    assert url == "https://hooks.example.test/ci"
    assert payload["event"] == "status_changed"
    assert payload["rule_name"] == "rule-task_status_changed"


def test_sprint_completed_auto_starts_the_next_planned_sprint_through_the_worker_path(
    factory: sessionmaker[Session],
) -> None:
    project_id, sprint_id = _seed_planned_sprint(factory)
    _add_rule(
        factory,
        project_id=project_id,
        trigger_type=AutomationTriggerType.SPRINT_COMPLETED,
        actions=[{"type": "start_sprint"}],
    )
    envelope = _envelope(
        trigger_type=AutomationTriggerType.SPRINT_COMPLETED,
        project_id=project_id,
        entity_id=sprint_id,
        entity_type=AutomationEntityType.SPRINT,
    )

    executions = evaluate_envelope(factory, envelope)

    assert len(executions) == 1
    result = executions[0].action_results[0]
    assert result["status"] == "ok"
    assert result["detail"]["sprint_id"] == str(sprint_id)
    with factory() as session:
        sprint = session.get(Sprint, sprint_id)
        assert sprint is not None
        assert sprint.status == SprintState.ACTIVE.value
