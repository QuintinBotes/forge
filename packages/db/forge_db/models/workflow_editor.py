"""ORM models for the workflow visual editor (F28).

Two workspace-scoped, append-only-ish tables that let an admin fork/author and
version a workflow definition in the DB so it overrides the bundled YAML at run
time. Status/source/validation are ``VARCHAR`` + ``CHECK`` (via ``enum_type``),
matching the foundation convention (no native Postgres ENUM types).

All ORM lives in ``packages/db`` so the single Alembic ``env.py`` sees it; the
``forge_workflow.editor`` subpackage imports these models for its repository.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from forge_db.base import WorkspaceScopedModel, enum_type, json_type


class WorkflowDefinitionSource(enum.StrEnum):
    """Where a DB workflow definition came from."""

    BUNDLED_FORK = "bundled_fork"
    CUSTOM = "custom"


class RevisionStatus(enum.StrEnum):
    """Lifecycle status of a definition revision."""

    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class RevisionValidationStatus(enum.StrEnum):
    """Last validation outcome stored on a revision."""

    VALID = "valid"
    INVALID = "invalid"
    UNVALIDATED = "unvalidated"


class WorkflowDefinition(WorkspaceScopedModel):
    """A named, workspace-scoped, editable workflow definition.

    A present-and-active row overrides the bundled file of the same name at
    resolution time.
    """

    __tablename__ = "workflow_definition"
    __table_args__ = (
        Index(
            "uq_workflow_definition_workspace_name",
            "workspace_id",
            "name",
            unique=True,
        ),
    )

    name: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[WorkflowDefinitionSource] = mapped_column(
        enum_type(WorkflowDefinitionSource), nullable=False
    )
    base_bundled_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Pointers to revisions. Plain UUID columns (no DB-level FK) to avoid a
    # circular FK with workflow_definition_revision; the repository enforces
    # referential integrity and revision rows CASCADE from this definition. (The
    # slice doc specifies FK SET NULL here — deviation noted for cross-dialect
    # create_all simplicity.)
    current_published_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    draft_revision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("app_user.id", ondelete="SET NULL"),
        nullable=True,
    )


class WorkflowDefinitionRevision(WorkspaceScopedModel):
    """An immutable revision snapshot of a definition.

    Append-only except the single draft, which may flip ``status`` and update its
    ``validation_*``/``published_at`` exactly once on publish (repository-enforced
    — not the DB immutability trigger, which forbids all UPDATE).
    """

    __tablename__ = "workflow_definition_revision"
    __table_args__ = (
        Index(
            "uq_workflow_definition_revision_revision",
            "workflow_definition_id",
            "revision",
            unique=True,
        ),
        # At most one draft per definition (partial unique).
        Index(
            "uq_workflow_definition_revision_one_draft",
            "workflow_definition_id",
            unique=True,
            postgresql_where=text("status = 'draft'"),
            sqlite_where=text("status = 'draft'"),
        ),
        Index(
            "ix_workflow_definition_revision_definition",
            "workflow_definition_id",
        ),
    )

    workflow_definition_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workflow_definition.id", ondelete="CASCADE"),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[RevisionStatus] = mapped_column(enum_type(RevisionStatus), nullable=False)
    dsl_yaml: Mapped[str] = mapped_column(Text, nullable=False)
    graph_json: Mapped[dict[str, Any]] = mapped_column(json_type(), nullable=False)
    dsl_version: Mapped[str] = mapped_column(String(16), default="1", nullable=False)
    validation_status: Mapped[RevisionValidationStatus] = mapped_column(
        enum_type(RevisionValidationStatus),
        default=RevisionValidationStatus.UNVALIDATED,
        server_default=RevisionValidationStatus.UNVALIDATED.value,
        nullable=False,
    )
    validation_issues: Mapped[list[Any]] = mapped_column(
        json_type(), default=list, server_default=text("'[]'"), nullable=False
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("app_user.id", ondelete="SET NULL"),
        nullable=True,
    )
    published_at: Mapped[datetime | None] = mapped_column(nullable=True)


__all__ = [
    "RevisionStatus",
    "RevisionValidationStatus",
    "WorkflowDefinition",
    "WorkflowDefinitionRevision",
    "WorkflowDefinitionSource",
]
