"""Risk classifications and gate observations (spec A §5.3, #193).

``risk_classification`` holds one row per classified agent change: its identity
(the ``attestation`` field set), the derived, declared and final class, the
matched rule ids in policy order, a snapshot of the change signals (paths and
counts only, never file contents), the enforcement mode in force and whether the
fallback class applied.

``risk_gate_observation`` holds one row per gate evaluation: what the gate
decided, what risk would have decided and whether they agreed. Agreement per gate
over a window is the evidence for switching a repository from shadow to enforcing.

Both are append-only at two layers, like ``attestation``: the repository
(``scf_db.risk_repository``) has no update or delete path, and each table opts into
:func:`~scf_db.base.attach_immutability_trigger` on Postgres. The run foreign keys
cascade for the reason ``attestation`` gives.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from scf_contracts import RiskClass
from scf_db.base import WorkspaceScopedModel, attach_immutability_trigger, enum_type, json_type


class RiskClassification(WorkspaceScopedModel):
    """One immutable risk classification of an agent change."""

    __tablename__ = "risk_classification"
    __table_args__ = (
        Index("ix_risk_classification_workspace_created", "workspace_id", "created_at"),
        Index("ix_risk_classification_commit_sha", "commit_sha"),
        # One classification per agent run (a retried close-out writes none); NULLs are distinct.
        Index("uq_risk_classification_agent_run_id", "agent_run_id", unique=True),
        Index("ix_risk_classification_workflow_run_id", "workflow_run_id"),
    )

    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    pr_numbers: Mapped[list[Any]] = mapped_column(json_type(), default=list, nullable=False)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_run.id", ondelete="CASCADE"), nullable=True
    )
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workflow_run.id", ondelete="CASCADE"), nullable=True
    )
    spec_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    spec_version: Mapped[int | None] = mapped_column(Integer, nullable=True)

    derived_class: Mapped[RiskClass] = mapped_column(enum_type(RiskClass), nullable=False)
    declared_class: Mapped[RiskClass | None] = mapped_column(enum_type(RiskClass), nullable=True)
    final_class: Mapped[RiskClass] = mapped_column(enum_type(RiskClass), nullable=False)
    #: Ids of the matching ``derive`` rules, in policy order.
    matched_rule_ids: Mapped[list[Any]] = mapped_column(json_type(), default=list, nullable=False)
    #: ``ChangeSignals`` with secret-shaped path text redacted: paths and counts only.
    signals: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict, nullable=False)
    override_approver: Mapped[str | None] = mapped_column(String(255), nullable=True)
    override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: ``shadow`` or ``enforcing``: the mode in force when the change was classified.
    enforcement_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    #: True when the diff could not be read and the policy's fallback class applied.
    fallback_applied: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: ``GateRequirements`` per class name, from the policy's ``risk_rules`` (#194): the
    #: gates read the requirements of the class a change ships as. Absent on older rows.
    class_requirements: Mapped[dict[str, Any] | None] = mapped_column(json_type(), nullable=True)


class RiskGateObservation(WorkspaceScopedModel):
    """One immutable record of what a shipping gate decided beside what risk would have."""

    __tablename__ = "risk_gate_observation"
    __table_args__ = (
        Index("ix_risk_gate_observation_workspace_created", "workspace_id", "created_at"),
        Index("ix_risk_gate_observation_classification_id", "classification_id"),
    )

    classification_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("risk_classification.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: ``deploy``, ``merge``, ``approval``, ``red_team`` or ``self_eval``.
    gate_name: Mapped[str] = mapped_column(String(32), nullable=False)
    actual_decision: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_decision: Mapped[str] = mapped_column(String(64), nullable=False)
    agreed: Mapped[bool] = mapped_column(Boolean, nullable=False)


# Postgres: append-only via the shared BEFORE UPDATE/DELETE trigger.
attach_immutability_trigger(RiskClassification.__table__)
attach_immutability_trigger(RiskGateObservation.__table__)


__all__ = ["RiskClassification", "RiskGateObservation"]
