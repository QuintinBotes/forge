"""Unit tests for the F40 automation-action wiring (F40-AUT-ACTIONS).

Covers the concrete ``ExternalActionExecutor`` ports the API layer supplies:
the audit bridge, the real incident-service + sprint-service wiring, the PM-
adapter issue bridge, and the deploy/merge mocks — hermetic (in-memory SQLite
+ fakes, no network, no Celery).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import sessionmaker

from forge_api.observability.audit import AuditCategory, AuditLog
from forge_api.services.automation_actions import (
    AutomationActionAuditSink,
    IncidentServiceDeclarer,
    MockDeployDispatcher,
    MockGitHubMergeDispatcher,
    PmAdapterIssueCreator,
    SprintServiceAutoStarter,
)
from forge_api.services.incident_service import IncidentService
from forge_board.automation.executor import ActionContext
from forge_board.automation.schemas import EntitySnapshot
from forge_board.sprint_service import SprintService
from forge_contracts.automation import (
    AutomationActionType,
    AutomationEntityType,
    AutomationTriggerEnvelope,
    AutomationTriggerSource,
    AutomationTriggerType,
)
from forge_contracts.enums import IncidentSeverity, SprintState
from forge_contracts.pm import ExternalTask, ForgeTask, PMProvider
from forge_db.base import Base
from forge_db.models import Project, Sprint, Workspace

WS_ID = uuid.uuid4()


def _ctx(*, project_id: uuid.UUID | None = None) -> ActionContext:
    entity_id = uuid.uuid4()
    envelope = AutomationTriggerEnvelope(
        trigger_type=AutomationTriggerType.TASK_SLA_BREACHED,
        trigger_source=AutomationTriggerSource.BOARD_ACTIVITY,
        trigger_event_id=uuid.uuid4(),
        workspace_id=WS_ID,
        project_id=project_id,
        entity_type=AutomationEntityType.TASK,
        entity_id=entity_id,
    )
    snapshot = EntitySnapshot(entity_type=AutomationEntityType.TASK, entity_id=entity_id, fields={})
    return ActionContext(
        rule_id=uuid.uuid4(), rule_name="test-rule", snapshot=snapshot, envelope=envelope, depth=0
    )


# --------------------------------------------------------------------------- #
# audit sink                                                                   #
# --------------------------------------------------------------------------- #


def test_audit_sink_records_integration_category_for_external_actions() -> None:
    audit_log = AuditLog()
    sink = AutomationActionAuditSink(audit_log, workspace_id=WS_ID)
    rule_id = uuid.uuid4()

    sink.record(
        action_type=AutomationActionType.WEBHOOK_POST,
        rule_id=rule_id,
        status="ok",
        detail={"url": "https://example.test"},
    )

    entries = audit_log.query(category=AuditCategory.INTEGRATION)
    assert len(entries) == 1
    assert entries[0].action == "webhook_post"
    assert entries[0].target == str(rule_id)
    assert entries[0].status == "ok"


def test_audit_sink_records_agent_action_category_for_internal_actions() -> None:
    audit_log = AuditLog()
    sink = AutomationActionAuditSink(audit_log)

    sink.record(
        action_type=AutomationActionType.DECLARE_INCIDENT,
        rule_id=uuid.uuid4(),
        status="forbidden",
        detail={},
    )

    assert len(audit_log.query(category=AuditCategory.AGENT_ACTION)) == 1
    assert len(audit_log.query(category=AuditCategory.INTEGRATION)) == 0


# --------------------------------------------------------------------------- #
# incident declarer                                                            #
# --------------------------------------------------------------------------- #


def test_incident_declarer_declares_a_real_incident() -> None:
    project_id = uuid.uuid4()
    declarer = IncidentServiceDeclarer(IncidentService(), project_id=project_id)

    key = declarer.declare_incident(
        title="SLA breach", severity=IncidentSeverity.HIGH, ctx=_ctx(project_id=project_id)
    )

    assert key.startswith("INC-")


# --------------------------------------------------------------------------- #
# sprint auto-starter                                                          #
# --------------------------------------------------------------------------- #


@pytest.fixture
def session_factory() -> sessionmaker:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_sprint_auto_starter_starts_the_oldest_planned_sprint(session_factory) -> None:
    project_id = uuid.uuid4()
    with session_factory() as session:
        session.add(Workspace(id=WS_ID, name="Acme", slug="acme"))
        session.flush()
        session.add(Project(id=project_id, workspace_id=WS_ID, name="Core", key="CORE"))
        session.flush()
        session.add(Sprint(workspace_id=WS_ID, project_id=project_id, name="Sprint 1"))
        session.commit()

    starter = SprintServiceAutoStarter(SprintService(session_factory), workspace_id=WS_ID)
    started_id = starter.start_sprint(ctx=_ctx(project_id=project_id))

    assert started_id is not None
    with session_factory() as session:
        sprint = session.get(Sprint, uuid.UUID(started_id))
        assert sprint.status == SprintState.ACTIVE.value


def test_sprint_auto_starter_is_no_op_without_a_planned_sprint(session_factory) -> None:
    project_id = uuid.uuid4()
    with session_factory() as session:
        session.add(Workspace(id=WS_ID, name="Acme", slug="acme"))
        session.flush()
        session.add(Project(id=project_id, workspace_id=WS_ID, name="Core", key="CORE"))
        session.commit()

    starter = SprintServiceAutoStarter(SprintService(session_factory), workspace_id=WS_ID)
    assert starter.start_sprint(ctx=_ctx(project_id=project_id)) is None


def test_sprint_auto_starter_is_no_op_without_a_project(session_factory) -> None:
    starter = SprintServiceAutoStarter(SprintService(session_factory), workspace_id=WS_ID)
    assert starter.start_sprint(ctx=_ctx(project_id=None)) is None


# --------------------------------------------------------------------------- #
# PM-adapter issue creator                                                     #
# --------------------------------------------------------------------------- #


class _FakePMAdapter:
    provider = PMProvider.asana

    def __init__(self) -> None:
        self.created: ForgeTask | None = None

    async def create_external(self, forge_task: ForgeTask) -> ExternalTask:
        self.created = forge_task
        return ExternalTask(
            provider=PMProvider.asana,
            external_id="EXT-9",
            external_key="EXT-9",
            url="https://asana.test/EXT-9",
            title=forge_task.title,
            status_name="To Do",
            external_updated_at=datetime.now(UTC),
        )


def test_pm_adapter_issue_creator_creates_via_the_resolved_adapter() -> None:
    adapter = _FakePMAdapter()
    creator = PmAdapterIssueCreator(lambda provider: adapter)

    external_id = creator.create_issue("asana", title="Escalate this", ctx=_ctx())

    assert external_id == "EXT-9"
    assert adapter.created is not None
    assert adapter.created.title == "Escalate this"


# --------------------------------------------------------------------------- #
# deploy / merge mocks                                                        #
# --------------------------------------------------------------------------- #


def test_mock_deploy_dispatcher_records_the_dispatch() -> None:
    dispatcher = MockDeployDispatcher()
    deploy_id = dispatcher.trigger_deploy(environment="staging", ref="abc123", ctx=_ctx())
    assert dispatcher.dispatched == [
        {"deploy_id": deploy_id, "environment": "staging", "ref": "abc123"}
    ]


def test_mock_github_merge_dispatcher_records_the_dispatch() -> None:
    dispatcher = MockGitHubMergeDispatcher()
    merge_id = dispatcher.merge(ref="abc123", method="squash", ctx=_ctx())
    assert dispatcher.merged == [{"merge_id": merge_id, "ref": "abc123", "method": "squash"}]
