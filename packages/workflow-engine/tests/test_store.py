"""Tests for the workflow run stores (plan Task 1.8).

The engine persists ``WorkflowRun`` rows through a ``WorkflowStore``. The
in-memory store is the unit-test backend; the SQLAlchemy store is the
Postgres-backed production store, here exercised against SQLite (no live DB) to
prove the DTO <-> ORM round-trip. Live Postgres integration runs in Phase 2.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import forge_db.models  # noqa: F401  (registers every table on Base.metadata)
from forge_contracts import RunStatus, WorkflowRun
from forge_db.base import Base
from forge_workflow.exceptions import WorkflowRunNotFoundError
from forge_workflow.store import (
    InMemoryWorkflowStore,
    SqlAlchemyWorkflowStore,
    WorkflowStore,
)


def test_inmemory_store_satisfies_protocol() -> None:
    assert isinstance(InMemoryWorkflowStore(), WorkflowStore)


def test_inmemory_create_assigns_id() -> None:
    store = InMemoryWorkflowStore()
    created = store.create(WorkflowRun(task_id=uuid.uuid4()))
    assert created.id is not None


def test_inmemory_get_and_update_round_trip() -> None:
    store = InMemoryWorkflowStore()
    created = store.create(WorkflowRun(task_id=uuid.uuid4()))
    assert created.id is not None

    fetched = store.get(created.id)
    assert fetched.id == created.id

    fetched.current_state = "executing"
    fetched.status = RunStatus.RUNNING
    store.update(fetched)
    assert store.get(created.id).current_state == "executing"


def test_inmemory_isolation_no_shared_mutation() -> None:
    # Mutating a returned DTO must not leak into the store without update().
    store = InMemoryWorkflowStore()
    created = store.create(WorkflowRun(task_id=uuid.uuid4()))
    assert created.id is not None
    created.current_state = "tampered"
    assert store.get(created.id).current_state != "tampered"


def test_inmemory_get_missing_raises() -> None:
    with pytest.raises(WorkflowRunNotFoundError):
        InMemoryWorkflowStore().get(uuid.uuid4())


@pytest.fixture
def sqlite_session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_sqlalchemy_store_round_trip(sqlite_session: Session) -> None:
    workspace_id = uuid.uuid4()
    store = SqlAlchemyWorkflowStore(sqlite_session, workspace_id=workspace_id)
    assert isinstance(store, WorkflowStore)

    created = store.create(WorkflowRun(task_id=uuid.uuid4(), current_state="created"))
    assert created.id is not None

    fetched = store.get(created.id)
    assert fetched.task_id == created.task_id
    assert fetched.current_state == "created"

    fetched.current_state = "executing"
    fetched.status = RunStatus.RUNNING
    fetched.context = {"retry_count": 2}
    store.update(fetched)

    reloaded = store.get(created.id)
    assert reloaded.current_state == "executing"
    assert reloaded.status == RunStatus.RUNNING
    assert reloaded.context == {"retry_count": 2}


def test_sqlalchemy_get_missing_raises(sqlite_session: Session) -> None:
    store = SqlAlchemyWorkflowStore(sqlite_session, workspace_id=uuid.uuid4())
    with pytest.raises(WorkflowRunNotFoundError):
        store.get(uuid.uuid4())
