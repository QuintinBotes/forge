"""F30 first-class team control unit: ``team``.

Teams own/restrict work and confer project access to their members. Optional
nesting (``parent_team_id``) is cycle-checked and depth-capped
(``MAX_TEAM_DEPTH``) in the authz service. Foundation deviation: the base
``team`` table does not exist in-tree (the baseline has no ``teams`` table), so
F30's migration creates it with the full F30 column set (singular table name
per the foundation convention; the slice doc says ``teams``).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from forge_db.base import WorkspaceScopedModel


class Team(WorkspaceScopedModel):
    """A team: a workspace control unit with membership + project access."""

    __tablename__ = "team"
    __table_args__ = (UniqueConstraint("workspace_id", "key"),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_team_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("team.id", ondelete="RESTRICT"),
        nullable=True,
    )
    archived_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("app_user.id", ondelete="SET NULL"),
        nullable=True,
    )
