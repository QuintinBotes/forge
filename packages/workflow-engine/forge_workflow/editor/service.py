"""WorkflowEditorService — the single entry point the router calls (F28).

Orchestrates fork/create/save-draft/validate/publish/diff/rollback/archive/import
over a :class:`WorkflowDefinitionRepository`, the :class:`RegistryCatalog`, the
bundled loader, and the protected invariants. Performs no I/O outside the repo +
the audit sink, so it is unit-testable with a fake repo.
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol
from uuid import UUID

from forge_contracts import WorkflowDefinition as WorkflowDefinitionDTO
from forge_db.models.workflow_editor import (
    RevisionValidationStatus,
    WorkflowDefinitionSource,
)
from forge_workflow.dsl import parse_definition
from forge_workflow.editor.catalog import CatalogResponse, RegistryCatalog
from forge_workflow.editor.diff import definition_diff
from forge_workflow.editor.errors import (
    BundledReadOnlyError,
    DefinitionNameConflictError,
    DefinitionNotFoundError,
    PublishBlockedError,
    RevisionNotFoundError,
)
from forge_workflow.editor.graph import (
    NodeLayout,
    StateNode,
    WorkflowGraph,
    definition_to_graph,
    graph_to_yaml,
    yaml_to_graph,
)
from forge_workflow.editor.schemas import (
    CreateDefinition,
    DefinitionDetail,
    DefinitionDiff,
    DefinitionSummary,
    ImportRequest,
    RevisionDetail,
    RevisionSummary,
    SaveDraftRequest,
)
from forge_workflow.editor.store import default_bundled_loader
from forge_workflow.editor.validation import (
    FEATURE_INVARIANTS,
    IssueCode,
    ProtectedInvariant,
    Severity,
    ValidationIssue,
    collect_validation_issues,
    error_count,
    has_errors,
    warning_count,
)
from forge_workflow.exceptions import WorkflowDefinitionError

# Audit action / resource names (additive; degrade to the existing audit log if
# F39's enums are absent — see slice notes).
AUDIT_RESOURCE_WORKFLOW_DEFINITION = "workflow_definition"
AUDIT_ACTION_PUBLISHED = "workflow_definition.published"
AUDIT_ACTION_ROLLED_BACK = "workflow_definition.rolled_back"
AUDIT_ACTION_ARCHIVED = "workflow_definition.archived"

_BUNDLED_NS = uuid.UUID("f28f28f2-8f28-4f28-8f28-f28f28f28f28")


class AuditSink(Protocol):
    """Critical, fail-closed audit emission for governance events."""

    def emit(
        self,
        *,
        action: str,
        resource_type: str,
        resource_id: str,
        workspace_id: UUID,
        actor: UUID | None,
        metadata: dict[str, Any],
    ) -> None: ...


class NullAuditSink:
    """No-op sink (used when no audit backend is wired)."""

    def emit(self, **_kwargs: Any) -> None:
        return None


class RecordingAuditSink:
    """A test double recording every emitted event."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


class WorkflowEditorService:
    """The editor's application service."""

    def __init__(
        self,
        repo: Any,
        catalog: RegistryCatalog | None = None,
        *,
        bundled_loader: Any = default_bundled_loader,
        invariants: list[ProtectedInvariant] | None = None,
        audit: AuditSink | None = None,
    ) -> None:
        self._repo = repo
        self._catalog = catalog or RegistryCatalog()
        self._bundled_loader = bundled_loader
        self._invariants = invariants if invariants is not None else FEATURE_INVARIANTS
        self._audit: AuditSink = audit or NullAuditSink()

    # -- catalog -------------------------------------------------------------- #

    def catalog(self, workspace_id: UUID) -> CatalogResponse:
        extra = self._custom_states(workspace_id)
        return self._catalog.build(workspace_id=workspace_id, extra_states=extra)

    def _custom_states(self, workspace_id: UUID) -> list[str]:
        states: list[str] = []
        for defn in self._repo.list_all(workspace_id):
            row = self._latest_content(defn)
            if row is None:
                continue
            for node in self._graph_from_row(row).nodes:
                if node.id not in states:
                    states.append(node.id)
        return states

    def _skill_names(self, workspace_id: UUID) -> set[str]:
        return set(self._catalog.build(workspace_id=workspace_id).skills)

    # -- listing / detail ----------------------------------------------------- #

    def list_definitions(self, workspace_id: UUID) -> list[DefinitionSummary]:
        summaries: list[DefinitionSummary] = []
        db_names: set[str] = set()
        for defn in self._repo.list_all(workspace_id):
            db_names.add(defn.name)
            summaries.append(self._definition_summary(defn))
        for name in self._bundled_names():
            if name not in db_names:
                summaries.append(self._bundled_summary(name))
        summaries.sort(key=lambda s: s.name)
        return summaries

    def get_definition(self, workspace_id: UUID, name: str) -> DefinitionDetail:
        defn = self._repo.get(workspace_id, name)
        if defn is not None:
            return self._definition_detail(defn)
        bundled = self._bundled_loader(name)
        if bundled is not None:
            return self._bundled_detail(name, bundled)
        raise DefinitionNotFoundError(name)

    # -- fork / create -------------------------------------------------------- #

    def fork_bundled(
        self, workspace_id: UUID, bundled_name: str, *, actor: UUID | None
    ) -> DefinitionDetail:
        if self._repo.get(workspace_id, bundled_name) is not None:
            raise DefinitionNameConflictError(bundled_name)
        bundled = self._bundled_loader(bundled_name)
        if bundled is None:
            raise DefinitionNotFoundError(bundled_name)
        graph = definition_to_graph(bundled, title=bundled_name.replace("_", " ").title())
        defn = self._repo.create_definition(
            workspace_id=workspace_id,
            name=bundled_name,
            title=graph.title,
            description=None,
            source=WorkflowDefinitionSource.BUNDLED_FORK,
            base_bundled_name=bundled_name,
            actor=actor,
        )
        self._create_draft(defn, graph, notes="Forked from bundled definition", actor=actor)
        self._repo.commit()
        return self._definition_detail(self._repo.get(workspace_id, bundled_name))

    def create_definition(
        self, workspace_id: UUID, body: CreateDefinition, *, actor: UUID | None
    ) -> DefinitionDetail:
        if self._repo.get(workspace_id, body.name) is not None:
            raise DefinitionNameConflictError(body.name)
        graph = body.graph or self._starter_graph(body.name, body.title)
        graph = graph.model_copy(update={"name": body.name, "title": body.title})
        base = body.name if self._bundled_loader(body.name) is not None else None
        source = (
            WorkflowDefinitionSource.BUNDLED_FORK
            if base
            else WorkflowDefinitionSource.CUSTOM
        )
        defn = self._repo.create_definition(
            workspace_id=workspace_id,
            name=body.name,
            title=body.title,
            description=body.description,
            source=source,
            base_bundled_name=base,
            actor=actor,
        )
        self._create_draft(defn, graph, notes="Initial draft", actor=actor)
        self._repo.commit()
        return self._definition_detail(self._repo.get(workspace_id, body.name))

    # -- draft / validate ----------------------------------------------------- #

    def save_draft(
        self, workspace_id: UUID, name: str, req: SaveDraftRequest, *, actor: UUID | None
    ) -> RevisionDetail:
        defn = self._require_editable(workspace_id, name)
        graph = req.graph.model_copy(update={"name": name})
        issues = self._validate(graph, defn)
        draft = self._repo.get_draft(defn.id)
        status = self._validation_status(issues)
        graph_json = graph.model_dump(mode="json")
        dsl_yaml = graph_to_yaml(graph)
        issues_json = [i.model_dump(mode="json") for i in issues]
        if draft is None:
            draft = self._repo.create_draft(
                defn,
                dsl_yaml=dsl_yaml,
                graph_json=graph_json,
                dsl_version=graph.version,
                notes=req.notes,
                validation_status=status,
                validation_issues=issues_json,
                actor=actor,
            )
        else:
            draft = self._repo.update_draft(
                draft,
                dsl_yaml=dsl_yaml,
                graph_json=graph_json,
                dsl_version=graph.version,
                notes=req.notes,
                validation_status=status,
                validation_issues=issues_json,
            )
        self._repo.commit()
        return self._revision_detail(draft)

    def validate_draft(self, workspace_id: UUID, name: str) -> list[ValidationIssue]:
        defn = self._require_editable(workspace_id, name)
        draft = self._repo.get_draft(defn.id)
        if draft is None:
            raise RevisionNotFoundError(name, "draft")
        graph = self._graph_from_row(draft)
        issues = self._validate(graph, defn)
        self._repo.set_validation(
            draft,
            validation_status=self._validation_status(issues),
            validation_issues=[i.model_dump(mode="json") for i in issues],
        )
        self._repo.commit()
        return issues

    # -- publish -------------------------------------------------------------- #

    def publish(
        self, workspace_id: UUID, name: str, *, actor: UUID | None
    ) -> RevisionDetail:
        defn = self._require_editable(workspace_id, name)
        draft = self._repo.get_draft(defn.id)
        if draft is None:
            raise RevisionNotFoundError(name, "draft")
        graph = self._graph_from_row(draft)
        issues = self._validate(graph, defn)
        errors = [i for i in issues if i.severity.value == "error"]
        if errors:
            raise PublishBlockedError(errors)
        # Belt-and-braces parity gate.
        try:
            parse_definition(draft.dsl_yaml)
        except WorkflowDefinitionError as exc:  # pragma: no cover - defensive
            raise PublishBlockedError(
                [
                    ValidationIssue(
                        code=IssueCode.DUPLICATE_EDGE,  # nearest structural code
                        severity=Severity.ERROR,
                        message=str(exc),
                    )
                ]
            ) from exc

        try:
            self._audit.emit(
                action=AUDIT_ACTION_PUBLISHED,
                resource_type=AUDIT_RESOURCE_WORKFLOW_DEFINITION,
                resource_id=str(defn.id),
                workspace_id=workspace_id,
                actor=actor,
                metadata={"name": name, "revision": draft.revision},
            )
            published = self._repo.publish_draft(defn, draft)
            self._repo.commit()
        except Exception:
            self._repo.rollback()
            raise
        return self._revision_detail(published)

    # -- revisions / diff ----------------------------------------------------- #

    def list_revisions(self, workspace_id: UUID, name: str) -> list[RevisionSummary]:
        defn = self._require_definition(workspace_id, name)
        return [self._revision_summary(r) for r in self._repo.list_revisions(defn.id)]

    def get_revision(
        self, workspace_id: UUID, name: str, revision: int
    ) -> RevisionDetail:
        defn = self._require_definition(workspace_id, name)
        row = self._repo.get_revision(defn.id, revision)
        if row is None:
            raise RevisionNotFoundError(name, revision)
        return self._revision_detail(row)

    def diff_revisions(
        self, workspace_id: UUID, name: str, frm: int, to: int
    ) -> DefinitionDiff:
        defn = self._require_definition(workspace_id, name)
        from_row = self._repo.get_revision(defn.id, frm)
        to_row = self._repo.get_revision(defn.id, to)
        if from_row is None:
            raise RevisionNotFoundError(name, frm)
        if to_row is None:
            raise RevisionNotFoundError(name, to)
        return definition_diff(
            self._graph_from_row(from_row),
            self._graph_from_row(to_row),
            from_revision=frm,
            to_revision=to,
        )

    # -- rollback / archive --------------------------------------------------- #

    def rollback(
        self, workspace_id: UUID, name: str, to_revision: int, *, actor: UUID | None
    ) -> RevisionDetail:
        defn = self._require_editable(workspace_id, name)
        target = self._repo.get_revision(defn.id, to_revision)
        if target is None:
            raise RevisionNotFoundError(name, to_revision)
        graph = self._graph_from_row(target)
        issues = self._validate(graph, defn)
        status = self._validation_status(issues)
        issues_json = [i.model_dump(mode="json") for i in issues]
        draft = self._repo.get_draft(defn.id)
        notes = f"Rolled back to revision {to_revision}"
        if draft is None:
            draft = self._repo.create_draft(
                defn,
                dsl_yaml=target.dsl_yaml,
                graph_json=dict(target.graph_json),
                dsl_version=target.dsl_version,
                notes=notes,
                validation_status=status,
                validation_issues=issues_json,
                actor=actor,
            )
        else:
            draft = self._repo.update_draft(
                draft,
                dsl_yaml=target.dsl_yaml,
                graph_json=dict(target.graph_json),
                dsl_version=target.dsl_version,
                notes=notes,
                validation_status=status,
                validation_issues=issues_json,
            )
        try:
            self._audit.emit(
                action=AUDIT_ACTION_ROLLED_BACK,
                resource_type=AUDIT_RESOURCE_WORKFLOW_DEFINITION,
                resource_id=str(defn.id),
                workspace_id=workspace_id,
                actor=actor,
                metadata={"name": name, "to_revision": to_revision},
            )
            self._repo.commit()
        except Exception:
            self._repo.rollback()
            raise
        return self._revision_detail(draft)

    def archive(self, workspace_id: UUID, name: str, *, actor: UUID | None) -> None:
        defn = self._require_editable(workspace_id, name)
        try:
            self._audit.emit(
                action=AUDIT_ACTION_ARCHIVED,
                resource_type=AUDIT_RESOURCE_WORKFLOW_DEFINITION,
                resource_id=str(defn.id),
                workspace_id=workspace_id,
                actor=actor,
                metadata={"name": name},
            )
            self._repo.set_active(defn, is_active=False)
            self._repo.commit()
        except Exception:
            self._repo.rollback()
            raise

    # -- import / export ------------------------------------------------------ #

    def import_yaml(
        self, workspace_id: UUID, req: ImportRequest, *, actor: UUID | None
    ) -> DefinitionDetail:
        if self._repo.get(workspace_id, req.name) is not None:
            raise DefinitionNameConflictError(req.name)
        graph = yaml_to_graph(req.dsl_yaml, title=req.title)
        graph = graph.model_copy(update={"name": req.name, "title": req.title})
        base = req.name if self._bundled_loader(req.name) is not None else None
        source = (
            WorkflowDefinitionSource.BUNDLED_FORK
            if base
            else WorkflowDefinitionSource.CUSTOM
        )
        defn = self._repo.create_definition(
            workspace_id=workspace_id,
            name=req.name,
            title=req.title,
            description=None,
            source=source,
            base_bundled_name=base,
            actor=actor,
        )
        self._create_draft(defn, graph, notes="Imported from YAML", actor=actor)
        self._repo.commit()
        return self._definition_detail(self._repo.get(workspace_id, req.name))

    def export_yaml(
        self, workspace_id: UUID, name: str, *, revision: int | None = None
    ) -> str:
        defn = self._repo.get(workspace_id, name)
        if defn is None:
            bundled = self._bundled_loader(name)
            if bundled is None:
                raise DefinitionNotFoundError(name)
            return graph_to_yaml(definition_to_graph(bundled, title=name))
        if revision is not None:
            row = self._repo.get_revision(defn.id, revision)
        elif defn.current_published_revision_id is not None:
            row = self._repo.get_revision_by_id(defn.current_published_revision_id)
        else:
            row = self._repo.get_draft(defn.id)
        if row is None:
            raise RevisionNotFoundError(name, revision)
        return row.dsl_yaml

    # -- internals ------------------------------------------------------------ #

    def _validate(self, graph: WorkflowGraph, defn: Any) -> list[ValidationIssue]:
        return collect_validation_issues(
            graph,
            vocabulary=self._catalog.vocabulary,
            skill_names=self._skill_names(defn.workspace_id),
            base_bundled_name=defn.base_bundled_name,
            invariants=self._invariants,
        )

    @staticmethod
    def _validation_status(issues: list[ValidationIssue]) -> RevisionValidationStatus:
        return (
            RevisionValidationStatus.INVALID
            if has_errors(issues)
            else RevisionValidationStatus.VALID
        )

    def _create_draft(
        self, defn: Any, graph: WorkflowGraph, *, notes: str | None, actor: UUID | None
    ) -> Any:
        issues = self._validate(graph, defn)
        return self._repo.create_draft(
            defn,
            dsl_yaml=graph_to_yaml(graph),
            graph_json=graph.model_dump(mode="json"),
            dsl_version=graph.version,
            notes=notes,
            validation_status=self._validation_status(issues),
            validation_issues=[i.model_dump(mode="json") for i in issues],
            actor=actor,
        )

    def _require_definition(self, workspace_id: UUID, name: str) -> Any:
        defn = self._repo.get(workspace_id, name)
        if defn is not None:
            return defn
        if self._bundled_loader(name) is not None:
            raise BundledReadOnlyError(name)
        raise DefinitionNotFoundError(name)

    def _require_editable(self, workspace_id: UUID, name: str) -> Any:
        defn = self._repo.get(workspace_id, name)
        if defn is not None:
            return defn
        if self._bundled_loader(name) is not None:
            raise BundledReadOnlyError(name)
        raise DefinitionNotFoundError(name)

    def _latest_content(self, defn: Any) -> Any:
        if defn.draft_revision_id is not None:
            row = self._repo.get_revision_by_id(defn.draft_revision_id)
            if row is not None:
                return row
        if defn.current_published_revision_id is not None:
            row = self._repo.get_revision_by_id(defn.current_published_revision_id)
            if row is not None:
                return row
        revisions = self._repo.list_revisions(defn.id)
        return revisions[-1] if revisions else None

    @staticmethod
    def _graph_from_row(row: Any) -> WorkflowGraph:
        return WorkflowGraph.model_validate(row.graph_json)

    # -- DTO builders --------------------------------------------------------- #

    def _revision_summary(self, row: Any) -> RevisionSummary:
        issues = [ValidationIssue.model_validate(i) for i in (row.validation_issues or [])]
        return RevisionSummary(
            id=row.id,
            revision=row.revision,
            status=row.status.value,
            validation_status=row.validation_status.value,
            error_count=error_count(issues),
            warning_count=warning_count(issues),
            notes=row.notes,
            created_by=row.created_by,
            created_at=row.created_at,
            published_at=row.published_at,
        )

    def _revision_detail(self, row: Any) -> RevisionDetail:
        issues = [ValidationIssue.model_validate(i) for i in (row.validation_issues or [])]
        return RevisionDetail(
            id=row.id,
            revision=row.revision,
            status=row.status.value,
            validation_status=row.validation_status.value,
            error_count=error_count(issues),
            warning_count=warning_count(issues),
            notes=row.notes,
            created_by=row.created_by,
            created_at=row.created_at,
            published_at=row.published_at,
            graph=WorkflowGraph.model_validate(row.graph_json),
            dsl_yaml=row.dsl_yaml,
            validation_issues=issues,
        )

    def _definition_summary(self, defn: Any) -> DefinitionSummary:
        published = None
        if defn.current_published_revision_id is not None:
            row = self._repo.get_revision_by_id(defn.current_published_revision_id)
            published = row.revision if row else None
        return DefinitionSummary(
            name=defn.name,
            title=defn.title,
            description=defn.description,
            origin=defn.source.value,
            base_bundled_name=defn.base_bundled_name,
            is_active=defn.is_active,
            published_revision=published,
            has_draft=defn.draft_revision_id is not None,
        )

    def _definition_detail(self, defn: Any) -> DefinitionDetail:
        summary = self._definition_summary(defn)
        published = None
        if defn.current_published_revision_id is not None:
            row = self._repo.get_revision_by_id(defn.current_published_revision_id)
            published = self._revision_detail(row) if row else None
        draft = None
        if defn.draft_revision_id is not None:
            row = self._repo.get_revision_by_id(defn.draft_revision_id)
            draft = self._revision_detail(row) if row else None
        return DefinitionDetail(
            **summary.model_dump(),
            editable=True,
            current_published=published,
            draft=draft,
        )

    # -- bundled (read-only) views ------------------------------------------- #

    def _bundled_names(self) -> list[str]:
        names = ["default_feature"]
        if self._bundled_loader("incident") is not None:
            names.append("incident")
        return names

    def _bundled_summary(self, name: str) -> DefinitionSummary:
        bundled = self._bundled_loader(name)
        title = name.replace("_", " ").title()
        return DefinitionSummary(
            name=name,
            title=title,
            description=None,
            origin="bundled",
            base_bundled_name=None,
            is_active=True,
            published_revision=0 if bundled else None,
            has_draft=False,
        )

    def _bundled_detail(self, name: str, bundled: WorkflowDefinitionDTO) -> DefinitionDetail:
        graph = definition_to_graph(bundled, title=name.replace("_", " ").title())
        detail = RevisionDetail(
            id=uuid.uuid5(_BUNDLED_NS, name),
            revision=0,
            status="published",
            validation_status="valid",
            error_count=0,
            warning_count=0,
            notes="Bundled (read-only) definition",
            created_by=None,
            created_at=None,
            published_at=None,
            graph=graph,
            dsl_yaml=graph_to_yaml(graph),
            validation_issues=[],
        )
        return DefinitionDetail(
            name=name,
            title=graph.title,
            description=None,
            origin="bundled",
            base_bundled_name=None,
            is_active=True,
            published_revision=0,
            has_draft=False,
            editable=False,
            current_published=detail,
            draft=None,
        )

    @staticmethod
    def _starter_graph(name: str, title: str) -> WorkflowGraph:
        return WorkflowGraph(
            name=name,
            title=title,
            nodes=[
                StateNode(id="created", kind="initial", layout=NodeLayout(x=0, y=0)),
            ],
            edges=[],
        )


__all__ = [
    "AUDIT_ACTION_ARCHIVED",
    "AUDIT_ACTION_PUBLISHED",
    "AUDIT_ACTION_ROLLED_BACK",
    "AUDIT_RESOURCE_WORKFLOW_DEFINITION",
    "AuditSink",
    "NullAuditSink",
    "RecordingAuditSink",
    "WorkflowEditorService",
]
