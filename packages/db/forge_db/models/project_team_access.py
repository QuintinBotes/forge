"""F30 per-project team access: ``project_team_access``.

A team's access level on a project (``read|write|admin`` -> viewer|member|admin).
Unique on ``(project_id, team_id)``.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Index, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from forge_db.base import WorkspaceScopedModel, enum_type
from forge_db.models.enums import AccessLevel


class ProjectTeamAccess(WorkspaceScopedModel):
    """A team's access level on a project."""

    __tablename__ = "project_team_access"
    __table_args__ = (
        UniqueConstraint("project_id", "team_id", name="uq_project_team_access_project_team"),
        Index("ix_project_team_access_team", "team_id"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("team.id", ondelete="CASCADE"), nullable=False
    )
    access_level: Mapped[AccessLevel] = mapped_column(enum_type(AccessLevel), nullable=False)
