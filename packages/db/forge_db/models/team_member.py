"""F30 team membership: ``team_member``.

A user's membership in a team with a team-role (``lead`` confers
``team.member.manage`` on that team). Unique on ``(team_id, user_id)``.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from forge_db.base import WorkspaceScopedModel, enum_type
from forge_db.models.enums import TeamRole


class TeamMember(WorkspaceScopedModel):
    """A user's membership in a team."""

    __tablename__ = "team_member"
    __table_args__ = (UniqueConstraint("team_id", "user_id", name="uq_team_member_team_user"),)

    team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("team.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    team_role: Mapped[TeamRole] = mapped_column(
        enum_type(TeamRole), default=TeamRole.MEMBER, nullable=False
    )
