"""Workflow visual editor (F28): graph model, validation, catalog, store + service.

Extends the foundation workflow engine with a governed, versioned, visual
authoring surface. Reuses F07's DSL types + loader wholesale and never
re-implements transition execution.
"""

from __future__ import annotations

from forge_workflow.editor.catalog import (
    CatalogResponse,
    EffectMeta,
    GuardMeta,
    RegistryCatalog,
    Vocabulary,
    scan_vocabulary,
)
from forge_workflow.editor.diff import definition_diff
from forge_workflow.editor.errors import (
    BundledReadOnlyError,
    DefinitionNameConflictError,
    DefinitionNotFoundError,
    PublishBlockedError,
    RevisionNotFoundError,
    UnknownDefinitionError,
)
from forge_workflow.editor.graph import (
    HUMAN_GATE_STATES,
    TERMINAL_STATES,
    NodeLayout,
    StateNode,
    TransitionEdge,
    WorkflowGraph,
    auto_layout,
    definition_to_graph,
    graph_to_definition,
    graph_to_yaml,
    yaml_to_graph,
)
from forge_workflow.editor.repository import DbWorkflowDefinitionRepository
from forge_workflow.editor.schemas import (
    CreateDefinition,
    DefinitionDetail,
    DefinitionDiff,
    DefinitionSummary,
    ImportRequest,
    RevisionDetail,
    RevisionSummary,
    RollbackRequest,
    SaveDraftRequest,
    TransitionDiff,
)
from forge_workflow.editor.service import (
    AuditSink,
    NullAuditSink,
    RecordingAuditSink,
    WorkflowEditorService,
)
from forge_workflow.editor.store import (
    DbWorkflowDefinitionStore,
    ResolvingDefinitionProvider,
    WorkflowDefinitionStore,
    default_bundled_loader,
)
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

__all__ = [
    "FEATURE_INVARIANTS",
    "HUMAN_GATE_STATES",
    "TERMINAL_STATES",
    "AuditSink",
    "BundledReadOnlyError",
    "CatalogResponse",
    "CreateDefinition",
    "DbWorkflowDefinitionRepository",
    "DbWorkflowDefinitionStore",
    "DefinitionDetail",
    "DefinitionDiff",
    "DefinitionNameConflictError",
    "DefinitionNotFoundError",
    "DefinitionSummary",
    "EffectMeta",
    "GuardMeta",
    "ImportRequest",
    "IssueCode",
    "NodeLayout",
    "NullAuditSink",
    "ProtectedInvariant",
    "PublishBlockedError",
    "RecordingAuditSink",
    "RegistryCatalog",
    "ResolvingDefinitionProvider",
    "RevisionDetail",
    "RevisionNotFoundError",
    "RevisionSummary",
    "RollbackRequest",
    "SaveDraftRequest",
    "Severity",
    "StateNode",
    "TransitionDiff",
    "TransitionEdge",
    "UnknownDefinitionError",
    "ValidationIssue",
    "Vocabulary",
    "WorkflowDefinitionStore",
    "WorkflowEditorService",
    "WorkflowGraph",
    "auto_layout",
    "collect_validation_issues",
    "default_bundled_loader",
    "definition_diff",
    "definition_to_graph",
    "error_count",
    "graph_to_definition",
    "graph_to_yaml",
    "has_errors",
    "scan_vocabulary",
    "warning_count",
    "yaml_to_graph",
]
