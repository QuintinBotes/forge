"""Run-time revision pinning through the engine (F28 AC 13)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session

from forge_workflow.editor.repository import DbWorkflowDefinitionRepository
from forge_workflow.editor.schemas import SaveDraftRequest
from forge_workflow.editor.service import WorkflowEditorService
from forge_workflow.editor.store import (
    DbWorkflowDefinitionStore,
    ResolvingDefinitionProvider,
)
from forge_workflow.engine import (
    DEFINITION_REVISION_KEY,
    DEFINITION_VERSION_KEY,
    WorkflowEngineImpl,
)
from forge_workflow.store import InMemoryWorkflowStore

from .conftest import make_user, make_workspace

pytestmark = pytest.mark.usefixtures("db_session")


def test_run_pins_published_revision_and_does_not_drift(db_session: Session) -> None:
    ws = make_workspace(db_session)
    actor = make_user(db_session, ws)
    service = WorkflowEditorService(DbWorkflowDefinitionRepository(db_session))
    service.fork_bundled(ws, "default_feature", actor=actor)
    first = service.publish(ws, "default_feature", actor=actor)
    db_session.commit()

    repo = DbWorkflowDefinitionRepository(db_session)
    provider = ResolvingDefinitionProvider(DbWorkflowDefinitionStore(repo))
    engine = WorkflowEngineImpl(InMemoryWorkflowStore(), definition_provider=provider)

    run = engine.start(uuid.uuid4(), "default_feature", workspace_id=ws)
    assert run.context[DEFINITION_REVISION_KEY] == str(first.id)
    assert run.context[DEFINITION_VERSION_KEY] == first.graph.version
    assert run.current_state == "created"

    # Publish a NEW revision while the run is in-flight.
    detail = service.get_definition(ws, "default_feature")
    graph = detail.current_published.graph.model_copy(deep=True)
    graph.retry_policy = graph.retry_policy.model_copy(update={"max_retries": 7})
    service.save_draft(ws, "default_feature", SaveDraftRequest(graph=graph), actor=actor)
    second = service.publish(ws, "default_feature", actor=actor)
    db_session.commit()
    assert second.id != first.id

    # The in-flight run keeps its pinned revision (no drift).
    pinned = engine.get_run(run.id)
    assert pinned.context[DEFINITION_REVISION_KEY] == str(first.id)
    # A transition still resolves against the pinned revision's graph.
    assert engine.transition(run.id, "generate_spec_draft") == "spec_drafting"

    # A brand-new run resolves the latest published revision.
    fresh = engine.start(uuid.uuid4(), "default_feature", workspace_id=ws)
    assert fresh.context[DEFINITION_REVISION_KEY] == str(second.id)


def test_engine_without_provider_is_unchanged() -> None:
    """F07 behavior is preserved when no provider is injected."""
    engine = WorkflowEngineImpl(InMemoryWorkflowStore())
    run = engine.start(uuid.uuid4())
    assert run.current_state == "created"
    assert DEFINITION_REVISION_KEY not in run.context
    assert engine.transition(run.id, "generate_spec_draft") == "spec_drafting"
