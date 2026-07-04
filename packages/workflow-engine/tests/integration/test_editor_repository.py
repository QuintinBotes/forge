"""Repository + resolver integration tests against Postgres (F28 AC 10, 12, 14)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from forge_db.models.workflow_editor import (
    RevisionStatus,
    RevisionValidationStatus,
    WorkflowDefinition,
    WorkflowDefinitionRevision,
    WorkflowDefinitionSource,
)
from forge_workflow.editor.repository import DbWorkflowDefinitionRepository
from forge_workflow.editor.schemas import SaveDraftRequest
from forge_workflow.editor.service import WorkflowEditorService
from forge_workflow.editor.store import (
    DbWorkflowDefinitionStore,
    ResolvingDefinitionProvider,
)

from .conftest import make_user, make_workspace

pytestmark = pytest.mark.usefixtures("db_session")


def _service(session: Session) -> WorkflowEditorService:
    return WorkflowEditorService(DbWorkflowDefinitionRepository(session))


def test_partial_unique_single_draft(db_session: Session) -> None:
    """AC 10: the DB rejects a second draft for the same definition."""
    ws = make_workspace(db_session)
    definition = WorkflowDefinition(
        id=uuid.uuid4(), workspace_id=ws, name="custom_flow", title="Custom",
        source=WorkflowDefinitionSource.CUSTOM,
    )
    db_session.add(definition)
    db_session.flush()
    for _ in range(2):
        db_session.add(
            WorkflowDefinitionRevision(
                id=uuid.uuid4(), workflow_definition_id=definition.id, workspace_id=ws,
                revision=db_session.query(WorkflowDefinitionRevision).count() + 1,
                status=RevisionStatus.DRAFT, dsl_yaml="workflow: x", graph_json={},
                dsl_version="1", validation_status=RevisionValidationStatus.UNVALIDATED,
                validation_issues=[],
            )
        )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_revisions_monotonic_and_immutable(db_session: Session) -> None:
    """AC 14: revisions strictly increase; published content is not mutated."""
    ws = make_workspace(db_session)
    actor = make_user(db_session, ws)
    service = _service(db_session)
    service.fork_bundled(ws, "default_feature", actor=actor)
    published = service.publish(ws, "default_feature", actor=actor)
    assert published.revision == 1
    original_yaml = service.get_revision(ws, "default_feature", 1).dsl_yaml

    rolled = service.rollback(ws, "default_feature", 1, actor=actor)
    assert rolled.revision == 2  # strictly increasing

    # revision 1 is untouched (still published, same content).
    rev1 = service.get_revision(ws, "default_feature", 1)
    assert rev1.status == "published"
    assert rev1.dsl_yaml == original_yaml


def test_resolver_db_over_bundled_two_workspaces(db_session: Session) -> None:
    """AC 12: DB definition overrides bundled in W1; W2 falls back to bundled."""
    ws1 = make_workspace(db_session)
    ws2 = make_workspace(db_session)
    actor = make_user(db_session, ws1)
    service = _service(db_session)
    service.fork_bundled(ws1, "default_feature", actor=actor)
    service.publish(ws1, "default_feature", actor=actor)
    db_session.commit()

    repo = DbWorkflowDefinitionRepository(db_session)
    provider = ResolvingDefinitionProvider(DbWorkflowDefinitionStore(repo))

    defn1, rev_id1, _version1 = provider.resolve("default_feature", workspace_id=ws1)
    assert rev_id1 is not None  # served from DB
    assert defn1.name == "default_feature"

    _defn2, rev_id2, _ = provider.resolve("default_feature", workspace_id=ws2)
    assert rev_id2 is None  # bundled fallback


def test_load_pinned_does_not_drift(db_session: Session) -> None:
    """AC 13 (resolver level): a pinned revision is stable across re-publish."""
    ws = make_workspace(db_session)
    actor = make_user(db_session, ws)
    service = _service(db_session)
    service.fork_bundled(ws, "default_feature", actor=actor)
    service.publish(ws, "default_feature", actor=actor)
    db_session.commit()

    repo = DbWorkflowDefinitionRepository(db_session)
    provider = ResolvingDefinitionProvider(DbWorkflowDefinitionStore(repo))
    _defn, pinned_rev_id, _ = provider.resolve("default_feature", workspace_id=ws)
    assert pinned_rev_id is not None
    pinned_before = provider.load_pinned(pinned_rev_id)

    # publish a NEW revision (drop an optional clarification edge so it differs)
    detail = service.get_definition(ws, "default_feature")
    graph = detail.current_published.graph.model_copy(deep=True)
    graph.edges = [
        e
        for e in graph.edges
        if not (e.from_state == "executing" and e.to_state == "needs_human_input")
    ]
    service.save_draft(ws, "default_feature", SaveDraftRequest(graph=graph), actor=actor)
    service.publish(ws, "default_feature", actor=actor)
    db_session.commit()

    _defn2, latest_rev_id, _ = provider.resolve("default_feature", workspace_id=ws)
    assert latest_rev_id != pinned_rev_id  # resolve now serves the new revision
    # the pinned revision still loads the original content
    pinned_after = provider.load_pinned(pinned_rev_id)
    assert pinned_after == pinned_before
