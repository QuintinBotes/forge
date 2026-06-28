"""SQLAlchemy repository for the workflow editor (F28).

Owns the revision lifecycle and enforces append-only semantics: revisions are
inserted, never their ``dsl_yaml``/``graph_json``/``created_*`` updated; only the
single draft may flip ``status``/``validation_*``/``published_at`` once on publish
(the DB immutability trigger is *not* used because the draft must legally mutate).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from forge_db.models.workflow_editor import (
    RevisionStatus,
    RevisionValidationStatus,
    WorkflowDefinition,
    WorkflowDefinitionRevision,
    WorkflowDefinitionSource,
)


def _now() -> datetime:
    return datetime.now(UTC)


class DbWorkflowDefinitionRepository:
    """Concrete repository over a SQLAlchemy :class:`Session`."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    def commit(self) -> None:
        self._session.commit()

    def rollback(self) -> None:
        self._session.rollback()

    def flush(self) -> None:
        self._session.flush()

    # -- definitions ---------------------------------------------------------- #

    def get(self, workspace_id: UUID, name: str) -> WorkflowDefinition | None:
        return self._session.scalars(
            select(WorkflowDefinition).where(
                WorkflowDefinition.workspace_id == workspace_id,
                WorkflowDefinition.name == name,
            )
        ).one_or_none()

    def list_all(self, workspace_id: UUID) -> list[WorkflowDefinition]:
        return list(
            self._session.scalars(
                select(WorkflowDefinition)
                .where(WorkflowDefinition.workspace_id == workspace_id)
                .order_by(WorkflowDefinition.name)
            )
        )

    def create_definition(
        self,
        *,
        workspace_id: UUID,
        name: str,
        title: str,
        description: str | None,
        source: WorkflowDefinitionSource,
        base_bundled_name: str | None,
        actor: UUID | None,
    ) -> WorkflowDefinition:
        definition = WorkflowDefinition(
            workspace_id=workspace_id,
            name=name,
            title=title,
            description=description,
            source=source,
            base_bundled_name=base_bundled_name,
            is_active=True,
            created_by=actor,
        )
        self._session.add(definition)
        self._session.flush()
        return definition

    def set_active(self, definition: WorkflowDefinition, *, is_active: bool) -> None:
        definition.is_active = is_active
        self._session.flush()

    # -- revisions ------------------------------------------------------------ #

    def get_revision_by_id(
        self, revision_id: UUID
    ) -> WorkflowDefinitionRevision | None:
        return self._session.get(WorkflowDefinitionRevision, revision_id)

    def get_revision(
        self, definition_id: UUID, revision: int
    ) -> WorkflowDefinitionRevision | None:
        return self._session.scalars(
            select(WorkflowDefinitionRevision).where(
                WorkflowDefinitionRevision.workflow_definition_id == definition_id,
                WorkflowDefinitionRevision.revision == revision,
            )
        ).one_or_none()

    def list_revisions(
        self, definition_id: UUID
    ) -> list[WorkflowDefinitionRevision]:
        return list(
            self._session.scalars(
                select(WorkflowDefinitionRevision)
                .where(
                    WorkflowDefinitionRevision.workflow_definition_id == definition_id
                )
                .order_by(WorkflowDefinitionRevision.revision)
            )
        )

    def get_draft(self, definition_id: UUID) -> WorkflowDefinitionRevision | None:
        return self._session.scalars(
            select(WorkflowDefinitionRevision).where(
                WorkflowDefinitionRevision.workflow_definition_id == definition_id,
                WorkflowDefinitionRevision.status == RevisionStatus.DRAFT,
            )
        ).one_or_none()

    def next_revision(self, definition_id: UUID) -> int:
        revisions = self.list_revisions(definition_id)
        return (max((r.revision for r in revisions), default=0)) + 1

    def create_draft(
        self,
        definition: WorkflowDefinition,
        *,
        dsl_yaml: str,
        graph_json: dict[str, Any],
        dsl_version: str,
        notes: str | None,
        validation_status: RevisionValidationStatus,
        validation_issues: list[Any],
        actor: UUID | None,
    ) -> WorkflowDefinitionRevision:
        revision = WorkflowDefinitionRevision(
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
        self._session.add(revision)
        self._session.flush()
        definition.draft_revision_id = revision.id
        self._session.flush()
        return revision

    def update_draft(
        self,
        draft: WorkflowDefinitionRevision,
        *,
        dsl_yaml: str,
        graph_json: dict[str, Any],
        dsl_version: str,
        notes: str | None,
        validation_status: RevisionValidationStatus,
        validation_issues: list[Any],
    ) -> WorkflowDefinitionRevision:
        # The draft is mutable until published (single working draft).
        draft.dsl_yaml = dsl_yaml
        draft.graph_json = graph_json
        draft.dsl_version = dsl_version
        draft.notes = notes
        draft.validation_status = validation_status
        draft.validation_issues = validation_issues
        self._session.flush()
        return draft

    def set_validation(
        self,
        draft: WorkflowDefinitionRevision,
        *,
        validation_status: RevisionValidationStatus,
        validation_issues: list[Any],
    ) -> None:
        draft.validation_status = validation_status
        draft.validation_issues = validation_issues
        self._session.flush()

    def publish_draft(
        self, definition: WorkflowDefinition, draft: WorkflowDefinitionRevision
    ) -> WorkflowDefinitionRevision:
        draft.status = RevisionStatus.PUBLISHED
        draft.validation_status = RevisionValidationStatus.VALID
        draft.published_at = _now()
        definition.current_published_revision_id = draft.id
        definition.draft_revision_id = None
        self._session.flush()
        return draft


__all__ = ["DbWorkflowDefinitionRepository"]
