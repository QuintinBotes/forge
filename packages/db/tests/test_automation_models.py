"""Postgres integration tests for the F21 automation models (AC7, AC13).

Exercises the real Postgres code paths the SQLite unit tests cannot: the
``(rule_id, trigger_event_id)`` idempotency unique key and the F39 immutability
trigger that blocks UPDATE/DELETE on the append-only ``automation_execution``
table. Uses the shared ``pg_engine`` fixture; skips (parked) without Postgres.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from forge_db.base import Base
from forge_db.models import (
    AutomationExecution,
    AutomationRule,
    Project,
    Workspace,
)
from forge_db.models.enums import (
    AutomationEntityType,
    AutomationExecutionStatus,
    AutomationTriggerSource,
    AutomationTriggerType,
)

pytestmark = pytest.mark.usefixtures("pg_engine")


@pytest.fixture
def factory(pg_engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    try:
        yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(pg_engine)


def _seed(session: Session) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    ws = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    session.add(ws)
    session.flush()
    project = Project(workspace_id=ws.id, name="Core", key=f"C{uuid.uuid4().hex[:4]}")
    session.add(project)
    session.flush()
    rule = AutomationRule(
        workspace_id=ws.id,
        project_id=project.id,
        name="r",
        trigger_type=AutomationTriggerType.WORKFLOW_STATE_CHANGED,
        trigger_config={"to_state": "merged"},
        condition={},
        actions=[{"type": "close_linked_spec_tasks"}],
    )
    session.add(rule)
    session.flush()
    return ws.id, project.id, rule.id


def _execution(ws_id: uuid.UUID, rule_id: uuid.UUID, event_id: uuid.UUID) -> AutomationExecution:
    return AutomationExecution(
        workspace_id=ws_id,
        rule_id=rule_id,
        rule_version=1,
        trigger_type=AutomationTriggerType.WORKFLOW_STATE_CHANGED,
        trigger_event_id=event_id,
        trigger_source=AutomationTriggerSource.WORKFLOW_TRANSITION,
        entity_type=AutomationEntityType.TASK,
        entity_id=uuid.uuid4(),
        status=AutomationExecutionStatus.SUCCEEDED,
        depth=0,
        idempotency_key=f"{rule_id}:{event_id}",
    )


def test_rule_roundtrip_and_dispatch_index(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id, project_id, _rid = _seed(session)
        rule = AutomationRule(
            workspace_id=ws_id,
            project_id=project_id,
            name="Close spec tasks on merge",
            trigger_type=AutomationTriggerType.WORKFLOW_STATE_CHANGED,
            trigger_config={"to_state": "merged"},
            condition={},
            actions=[{"type": "close_linked_spec_tasks"}],
        )
        session.add(rule)
        session.commit()
        assert session.get(AutomationRule, rule.id).version == 1


def test_idempotency_unique_key(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id, _, rule_id = _seed(session)
        event_id = uuid.uuid4()
        session.add(_execution(ws_id, rule_id, event_id))
        session.commit()
        with pytest.raises(IntegrityError):
            session.add(_execution(ws_id, rule_id, event_id))
            session.commit()
        session.rollback()


def test_execution_is_immutable(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id, _, rule_id = _seed(session)
        row = _execution(ws_id, rule_id, uuid.uuid4())
        session.add(row)
        session.commit()
        row_id = row.id

    # A direct UPDATE is blocked by the immutability trigger.
    with factory() as session:
        with pytest.raises((ProgrammingError, IntegrityError)):
            session.execute(
                update(AutomationExecution)
                .where(AutomationExecution.id == row_id)
                .values(error="tampered")
            )
            session.commit()
        session.rollback()

    # A direct DELETE is likewise blocked.
    with factory() as session:
        with pytest.raises((ProgrammingError, IntegrityError)):
            session.execute(
                text("DELETE FROM automation_execution WHERE id = :i"), {"i": str(row_id)}
            )
            session.commit()
        session.rollback()
