"""Project container models: Project, Constitution."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from forge_db.base import WorkspaceScopedModel, enum_type, json_type
from forge_db.models.enums import ProjectVisibility

if TYPE_CHECKING:
    from forge_db.models.planning import Epic, Incident, Milestone, Sprint, Task
    from forge_db.models.workspace import Workspace


class Project(WorkspaceScopedModel):
    """A project: the container for epics, tasks, sprints, milestones, incidents."""

    __tablename__ = "project"
    __table_args__ = (UniqueConstraint("workspace_id", "key"),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    settings: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict, nullable=False)
    # F30: project visibility + optional owning team (multi-team controls).
    visibility: Mapped[ProjectVisibility] = mapped_column(
        enum_type(ProjectVisibility),
        default=ProjectVisibility.WORKSPACE,
        server_default=ProjectVisibility.WORKSPACE.value,
        nullable=False,
    )
    # Plain column on the model (no ORM-level FK) so SQLite can drop it on
    # downgrade; the ``team`` FK (ON DELETE SET NULL) is added on Postgres by the
    # F30 migration, mirroring 0009's child-run FK pattern.
    owner_team_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        nullable=True,
    )

    workspace: Mapped[Workspace] = relationship(back_populates="projects")
    constitution: Mapped[Constitution | None] = relationship(
        back_populates="project", cascade="all, delete-orphan", uselist=False
    )
    epics: Mapped[list[Epic]] = relationship(back_populates="project", cascade="all, delete-orphan")
    tasks: Mapped[list[Task]] = relationship(back_populates="project", cascade="all, delete-orphan")
    incidents: Mapped[list[Incident]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    sprints: Mapped[list[Sprint]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    milestones: Mapped[list[Milestone]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class Constitution(WorkspaceScopedModel):
    """Engineering principles + architecture guardrails for a project (1:1)."""

    __tablename__ = "constitution"

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    principles: Mapped[list[Any]] = mapped_column(json_type(), default=list, nullable=False)
    version: Mapped[str] = mapped_column(String(32), default="1", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)

    project: Mapped[Project] = relationship(back_populates="constitution")
