"""API DTOs for the workflow visual editor (F28).

Naming: the DB ``revision`` is a monotonic integer edit counter; the DSL
``version`` is the author-set semantic string. The UI labels revisions "versions".
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from forge_workflow.editor.graph import TransitionEdge, WorkflowGraph
from forge_workflow.editor.validation import ValidationIssue

RevisionStatus = Literal["draft", "published", "archived"]
ValidationState = Literal["valid", "invalid", "unvalidated"]
DefinitionOrigin = Literal["bundled", "bundled_fork", "custom"]


class RevisionSummary(BaseModel):
    id: UUID
    revision: int
    status: RevisionStatus
    validation_status: ValidationState
    error_count: int = 0
    warning_count: int = 0
    notes: str | None = None
    created_by: UUID | None = None
    created_at: datetime | None = None
    published_at: datetime | None = None


class RevisionDetail(RevisionSummary):
    graph: WorkflowGraph
    dsl_yaml: str
    validation_issues: list[ValidationIssue] = Field(default_factory=list)


class DefinitionSummary(BaseModel):
    name: str
    title: str
    description: str | None = None
    origin: DefinitionOrigin
    base_bundled_name: str | None = None
    is_active: bool = True
    published_revision: int | None = None
    has_draft: bool = False


class DefinitionDetail(DefinitionSummary):
    editable: bool = False
    current_published: RevisionDetail | None = None
    draft: RevisionDetail | None = None


class CreateDefinition(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,62}$")
    title: str = Field(min_length=1, max_length=160)
    description: str | None = None
    graph: WorkflowGraph | None = None


class SaveDraftRequest(BaseModel):
    graph: WorkflowGraph
    notes: str | None = None


class ImportRequest(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,62}$")
    title: str = Field(min_length=1, max_length=160)
    dsl_yaml: str = Field(max_length=200_000)


class RollbackRequest(BaseModel):
    to_revision: int = Field(ge=1)


class TransitionDiff(BaseModel):
    change: Literal["added", "removed", "changed"]
    from_state: str
    on: str
    to: str
    before: TransitionEdge | None = None
    after: TransitionEdge | None = None


class DefinitionDiff(BaseModel):
    name: str
    from_revision: int
    to_revision: int
    transition_diffs: list[TransitionDiff] = Field(default_factory=list)
    states_added: list[str] = Field(default_factory=list)
    states_removed: list[str] = Field(default_factory=list)
    policy_changed: bool = False


__all__ = [
    "CreateDefinition",
    "DefinitionDetail",
    "DefinitionDiff",
    "DefinitionOrigin",
    "DefinitionSummary",
    "ImportRequest",
    "RevisionDetail",
    "RevisionStatus",
    "RevisionSummary",
    "RollbackRequest",
    "SaveDraftRequest",
    "TransitionDiff",
    "ValidationState",
]
