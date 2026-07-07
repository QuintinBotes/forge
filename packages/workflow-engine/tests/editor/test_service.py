"""WorkflowEditorService unit tests with a fake repo (F28 AC 8-11, 16, 17)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from forge_db.models.workflow_editor import (
    RevisionStatus,
    RevisionValidationStatus,
    WorkflowDefinition,
    WorkflowDefinitionRevision,
)
from forge_workflow.editor.errors import (
    BundledReadOnlyError,
    PublishBlockedError,
)
from forge_workflow.editor.schemas import (
    CreateDefinition,
    ImportRequest,
    SaveDraftRequest,
)
from forge_workflow.editor.service import RecordingAuditSink, WorkflowEditorService

WS = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
ACTOR = uuid.UUID("00000000-0000-0000-0000-0000000000b2")


class FakeRepo:
    """In-memory repo over transient ORM instances (same attribute surface)."""

    def __init__(self) -> None:
        self.definitions: dict[uuid.UUID, WorkflowDefinition] = {}
        self.revisions: dict[uuid.UUID, WorkflowDefinitionRevision] = {}
        self.session = None
        self.committed = 0
        self.rolled_back = 0

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1

    def flush(self) -> None:
        return None

    def get(self, workspace_id, name):
        for d in self.definitions.values():
            if d.workspace_id == workspace_id and d.name == name:
                return d
        return None

    def list_all(self, workspace_id):
        return [d for d in self.definitions.values() if d.workspace_id == workspace_id]

    def create_definition(
        self, *, workspace_id, name, title, description, source, base_bundled_name, actor
    ):
        defn = WorkflowDefinition(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            name=name,
            title=title,
            description=description,
            source=source,
            base_bundled_name=base_bundled_name,
            is_active=True,
            created_by=actor,
        )
        self.definitions[defn.id] = defn
        return defn

    def set_active(self, definition, *, is_active):
        definition.is_active = is_active

    def get_revision_by_id(self, revision_id):
        return self.revisions.get(revision_id)

    def get_revision(self, definition_id, revision):
        for r in self.revisions.values():
            if r.workflow_definition_id == definition_id and r.revision == revision:
                return r
        return None

    def list_revisions(self, definition_id):
        rows = [r for r in self.revisions.values() if r.workflow_definition_id == definition_id]
        return sorted(rows, key=lambda r: r.revision)

    def get_draft(self, definition_id):
        for r in self.list_revisions(definition_id):
            if r.status == RevisionStatus.DRAFT:
                return r
        return None

    def next_revision(self, definition_id):
        return max((r.revision for r in self.list_revisions(definition_id)), default=0) + 1

    def create_draft(
        self,
        definition,
        *,
        dsl_yaml,
        graph_json,
        dsl_version,
        notes,
        validation_status,
        validation_issues,
        actor,
    ):
        rev = WorkflowDefinitionRevision(
            id=uuid.uuid4(),
            workflow_definition_id=definition.id,
            workspace_id=definition.workspace_id,
            revision=self.next_revision(definition.id),
            status=RevisionStatus.DRAFT,
            dsl_yaml=dsl_yaml,
            graph_json=graph_json,
            dsl_version=dsl_version,
            validation_status=validation_status,
            validation_issues=validation_issues,
            notes=notes,
            created_by=actor,
        )
        self.revisions[rev.id] = rev
        definition.draft_revision_id = rev.id
        return rev

    def update_draft(
        self,
        draft,
        *,
        dsl_yaml,
        graph_json,
        dsl_version,
        notes,
        validation_status,
        validation_issues,
    ):
        draft.dsl_yaml = dsl_yaml
        draft.graph_json = graph_json
        draft.dsl_version = dsl_version
        draft.notes = notes
        draft.validation_status = validation_status
        draft.validation_issues = validation_issues
        return draft

    def set_validation(self, draft, *, validation_status, validation_issues):
        draft.validation_status = validation_status
        draft.validation_issues = validation_issues

    def publish_draft(self, definition, draft):
        from datetime import UTC, datetime

        draft.status = RevisionStatus.PUBLISHED
        draft.validation_status = RevisionValidationStatus.VALID
        draft.published_at = datetime.now(UTC)
        definition.current_published_revision_id = draft.id
        definition.draft_revision_id = None
        return draft


@pytest.fixture
def audit() -> RecordingAuditSink:
    return RecordingAuditSink()


@pytest.fixture
def service(audit: RecordingAuditSink) -> WorkflowEditorService:
    return WorkflowEditorService(FakeRepo(), audit=audit)


def _find_edge(graph, frm, to):
    for e in graph.edges:
        if e.from_state == frm and e.to_state == to:
            return e
    raise AssertionError(f"no edge {frm} -> {to}")


def test_fork_creates_editable_draft(service: WorkflowEditorService) -> None:
    """AC 8: fork creates a bundled_fork definition with revision-1 draft."""
    detail = service.fork_bundled(WS, "default_feature", actor=ACTOR)
    assert detail.origin == "bundled_fork"
    assert detail.base_bundled_name == "default_feature"
    assert detail.editable is True
    assert detail.draft is not None
    assert detail.draft.revision == 1
    assert detail.draft.status == "draft"
    # the draft graph equals the bundled graph
    from forge_workflow.default_workflow import default_feature_definition
    from forge_workflow.editor.graph import definition_to_graph

    expected = definition_to_graph(default_feature_definition(), title="Default Feature")
    assert detail.draft.graph.edges == expected.edges


def test_bundled_is_read_only(service: WorkflowEditorService) -> None:
    """AC 9: saving a draft on the unforked bundled name is rejected."""
    detail = service.get_definition(WS, "default_feature")
    assert detail.origin == "bundled"
    assert detail.editable is False
    with pytest.raises(BundledReadOnlyError):
        service.save_draft(
            WS,
            "default_feature",
            SaveDraftRequest(graph=detail.current_published.graph),
            actor=ACTOR,
        )


def test_single_draft_upsert(service: WorkflowEditorService) -> None:
    """AC 10: two save_draft calls update the same draft revision."""
    service.fork_bundled(WS, "default_feature", actor=ACTOR)
    detail = service.get_definition(WS, "default_feature")
    graph = detail.draft.graph
    r1 = service.save_draft(WS, "default_feature", SaveDraftRequest(graph=graph), actor=ACTOR)
    r2 = service.save_draft(WS, "default_feature", SaveDraftRequest(graph=graph), actor=ACTOR)
    assert r1.id == r2.id
    assert r1.revision == r2.revision == 1


def test_publish_happy_path(service: WorkflowEditorService, audit: RecordingAuditSink) -> None:
    """AC 11: publishing a clean draft flips it published + emits audit."""
    service.fork_bundled(WS, "default_feature", actor=ACTOR)
    published = service.publish(WS, "default_feature", actor=ACTOR)
    assert published.status == "published"
    assert published.published_at is not None
    detail = service.get_definition(WS, "default_feature")
    assert detail.published_revision == 1
    assert detail.draft is None
    assert any(e["action"] == "workflow_definition.published" for e in audit.events)


def test_publish_blocked_on_errors(
    service: WorkflowEditorService, audit: RecordingAuditSink
) -> None:
    """AC 11: a draft with ERRORs cannot be published (409) and emits no audit."""
    service.fork_bundled(WS, "default_feature", actor=ACTOR)
    detail = service.get_definition(WS, "default_feature")
    graph = detail.draft.graph.model_copy(deep=True)
    edge = _find_edge(graph, "awaiting_review", "merged")
    edge.when = [s for s in edge.when if s != "review_approved_by_human"]
    service.save_draft(WS, "default_feature", SaveDraftRequest(graph=graph), actor=ACTOR)
    with pytest.raises(PublishBlockedError) as exc:
        service.publish(WS, "default_feature", actor=ACTOR)
    assert any(i.invariant_id == "merge_human_gate" for i in exc.value.errors)
    assert not any(e["action"] == "workflow_definition.published" for e in audit.events)


def test_publish_fail_closed_on_audit_error() -> None:
    """AC 11: a failing audit sink aborts the publish (fail-closed)."""

    class FailingAudit:
        def emit(self, **_kwargs: Any) -> None:
            raise RuntimeError("audit down")

    repo = FakeRepo()
    service = WorkflowEditorService(repo, audit=FailingAudit())
    service.fork_bundled(WS, "default_feature", actor=ACTOR)
    with pytest.raises(RuntimeError):
        service.publish(WS, "default_feature", actor=ACTOR)
    detail = service.get_definition(WS, "default_feature")
    # publish rolled back: still a draft, nothing published
    assert detail.published_revision is None
    assert detail.draft is not None
    assert detail.draft.status == "draft"


def test_rollback_creates_new_draft(
    service: WorkflowEditorService, audit: RecordingAuditSink
) -> None:
    """AC 16: rollback creates a new draft from the target revision's content."""
    service.fork_bundled(WS, "default_feature", actor=ACTOR)
    service.publish(WS, "default_feature", actor=ACTOR)  # revision 1 published
    rolled = service.rollback(WS, "default_feature", 1, actor=ACTOR)
    assert rolled.status == "draft"
    assert rolled.revision == 2
    # history row 1 untouched (still published)
    rev1 = service.get_revision(WS, "default_feature", 1)
    assert rev1.status == "published"
    assert any(e["action"] == "workflow_definition.rolled_back" for e in audit.events)


def test_import_validates_unregistered_effect(service: WorkflowEditorService) -> None:
    """AC 17: importing YAML with an unregistered effect yields an ERROR draft."""
    bad_yaml = """
workflow: imported_flow
version: "1"
transitions:
  - from: created
    to: closed
    action: definitely_not_a_real_effect
"""
    detail = service.import_yaml(
        WS,
        ImportRequest(name="imported_flow", title="Imported", dsl_yaml=bad_yaml),
        actor=ACTOR,
    )
    assert detail.draft is not None
    codes = {i.code for i in detail.draft.validation_issues}
    assert any(c == "unregistered_effect" for c in codes)
    with pytest.raises(PublishBlockedError):
        service.publish(WS, "imported_flow", actor=ACTOR)


def test_create_custom_definition(service: WorkflowEditorService) -> None:
    detail = service.create_definition(
        WS,
        CreateDefinition(name="release_train", title="Release Train"),
        actor=ACTOR,
    )
    assert detail.origin == "custom"
    assert detail.base_bundled_name is None
    assert detail.draft is not None
