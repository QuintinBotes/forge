"""Reusable template models: PolicyProfile, SkillProfile."""

from __future__ import annotations

from typing import Any

from sqlalchemy import String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from forge_db.base import WorkspaceScopedModel, json_type


class PolicyProfile(WorkspaceScopedModel):
    """A reusable ``.forge/policy.yaml``-shaped policy template."""

    __tablename__ = "policy_profile"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    document: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict, nullable=False)


class SkillProfile(WorkspaceScopedModel):
    """A reusable skill/behavior template (e.g. backend-tdd, incident-response)."""

    __tablename__ = "skill_profile"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    behavior: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict, nullable=False)
