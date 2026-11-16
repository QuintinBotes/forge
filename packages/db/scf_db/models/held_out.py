"""Held-out acceptance scenarios: checks the builder agent never sees (#176).

A scenario belongs to a task: files overlaid on the change's checkout and a
command run there. It is a draft until a person other than its drafter approves
it, and only approved scenarios are run. No agent-facing surface reads this
table: not the context assembler, the MCP tools, the agent-run routes or the
builder's objective.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from scf_db.base import WorkspaceScopedModel, json_type

#: A scenario is a draft until a person approves it.
SCENARIO_DRAFT = "draft"
SCENARIO_APPROVED = "approved"


class HeldOutScenario(WorkspaceScopedModel):
    """One executable acceptance scenario for a task, hidden from the builder."""

    __tablename__ = "held_out_scenario"

    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("task.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    #: ``[{"path": <path inside the checkout>, "content": <text>}]``.
    files: Mapped[list[dict[str, Any]]] = mapped_column(json_type(), nullable=False)
    command: Mapped[str] = mapped_column(Text, nullable=False)
    #: Who drafted it: ``user:<id>`` for a person, ``key:<id>`` for another credential.
    drafted_by: Mapped[str] = mapped_column(String(255), nullable=False)
    #: The user behind the drafting credential, so they cannot approve it another way.
    drafted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default=SCENARIO_DRAFT)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # never the files or the command
        return f"HeldOutScenario(id={self.id!r}, task_id={self.task_id!r}, state={self.state!r})"


__all__ = ["SCENARIO_APPROVED", "SCENARIO_DRAFT", "HeldOutScenario"]
