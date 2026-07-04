"""Automation models (F21): ``automation_rule`` + ``automation_execution``.

* ``automation_rule`` — a saved ``WHEN trigger IF condition THEN actions`` rule,
  project- or workspace-scoped, with an optimistic ``version`` and a fast
  partial dispatch index ``(workspace_id, project_id, trigger_type) WHERE enabled``.
* ``automation_execution`` — the append-only audit row written per firing. It is
  hardened DB-side via :func:`attach_immutability_trigger` (Postgres BEFORE
  UPDATE/DELETE block) and deduped by the ``(rule_id, trigger_event_id)``
  idempotency key, so a rule fires at most once per source event even under
  at-least-once redelivery.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from forge_db.base import WorkspaceScopedModel, attach_immutability_trigger, enum_type, json_type
from forge_db.models.enums import (
    AutomationEntityType,
    AutomationExecutionStatus,
    AutomationTriggerSource,
    AutomationTriggerType,
)


class AutomationRule(WorkspaceScopedModel):
    """A saved automation rule (project- or workspace-scoped)."""

    __tablename__ = "automation_rule"
    __table_args__ = (
        Index(
            "ix_automation_rule_dispatch",
            "workspace_id",
            "project_id",
            "trigger_type",
            postgresql_where=text("enabled"),
        ),
    )

    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    trigger_type: Mapped[AutomationTriggerType] = mapped_column(
        enum_type(AutomationTriggerType), nullable=False
    )
    trigger_config: Mapped[dict[str, Any]] = mapped_column(
        json_type(), default=dict, nullable=False
    )
    condition: Mapped[dict[str, Any]] = mapped_column(
        json_type(), default=dict, nullable=False
    )
    actions: Mapped[list[Any]] = mapped_column(json_type(), default=list, nullable=False)
    run_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class AutomationExecution(WorkspaceScopedModel):
    """An append-only audit row recorded for every automation firing."""

    __tablename__ = "automation_execution"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_automation_execution_idempotency_key"),
        Index("ix_automation_execution_rule_id_created", "rule_id", "created_at"),
        Index(
            "ix_automation_execution_entity", "entity_type", "entity_id", "created_at"
        ),
    )

    # Intentionally NOT a hard FK: the audit row is immutable (the F39 trigger
    # blocks UPDATE), so an ``ON DELETE SET NULL`` could never fire. The row
    # therefore simply outlives its rule — the audit survives rule deletion with
    # the original ``rule_id`` retained. (Deviation from the slice's literal
    # ``SET NULL`` to honor the stronger append-only guarantee.)
    rule_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    rule_version: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger_type: Mapped[AutomationTriggerType] = mapped_column(
        enum_type(AutomationTriggerType), nullable=False
    )
    trigger_event_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    trigger_source: Mapped[AutomationTriggerSource] = mapped_column(
        enum_type(AutomationTriggerSource), nullable=False
    )
    entity_type: Mapped[AutomationEntityType] = mapped_column(
        enum_type(AutomationEntityType), nullable=False
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[AutomationExecutionStatus] = mapped_column(
        enum_type(AutomationExecutionStatus), nullable=False
    )
    condition_result: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    actions_planned: Mapped[list[Any]] = mapped_column(
        json_type(), default=list, nullable=False
    )
    action_results: Mapped[list[Any]] = mapped_column(
        json_type(), default=list, nullable=False
    )
    depth: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    causation_chain: Mapped[list[Any]] = mapped_column(
        json_type(), default=list, nullable=False
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)


# Harden the audit table DB-side (Postgres BEFORE UPDATE/DELETE block; no-op on
# SQLite, where the repository exposes no update/delete path).
attach_immutability_trigger(AutomationExecution.__table__)


__all__ = ["AutomationExecution", "AutomationRule"]
