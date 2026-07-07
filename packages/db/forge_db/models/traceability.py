"""F23 spec-validation-dashboard projection tables (denormalised read model).

Two derived tables power the project-scoped Spec Validation Dashboard's
sub-100ms reads. Both are **derived state**: they can be dropped and fully
rebuilt from F02 (+ F08, when present) source rows by the
``TraceabilityProjector`` (``forge_spec.projection``). Nothing reads them as a
source of truth; gate decisions (F02/F08) never consult them.

* :class:`TraceabilityCriterionLink` — one row per acceptance criterion per
  spec; rebuilt wholesale per spec on every refresh.
* :class:`TraceabilitySpecRollup` — one row per spec; powers the project table in
  a single indexed scan, with a monotonic ``projection_version`` the UI's
  "Recompute" polls to detect completion.

Foundation deviations (conformed to the real foundation, not the slice doc):

* The slice doc places these ORM models in ``packages/spec-engine``; the
  foundation registers **every** model on ``forge_db``'s ``Base.metadata``
  (Alembic autogenerate + ``create_all``), so they live here.
* ``status`` / ``validation_status`` are stored as ``String`` (not a native
  ``traceability_cell_status`` enum): the foundation's free-string status columns
  precedent (``epic.status``, ``sprint.status``) and avoiding a ``forge_db ->
  forge_spec`` import cycle. The string values match
  ``forge_spec.dashboard_schemas.CellStatus`` / ``ValidationStatus`` verbatim.
* FKs target the real foundation tables (``spec_document``, ``project``); there
  is no ``spec_validation_reports`` table in this foundation, so the slice doc's
  ``last_report_id`` FK and ``spec_validation_reports.spec_version`` column are
  omitted (``report_spec_version`` is carried as a plain int instead).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from forge_db.base import WorkspaceScopedModel, json_type


class TraceabilityCriterionLink(WorkspaceScopedModel):
    """One acceptance-criterion projection row (grain = AC per spec)."""

    __tablename__ = "traceability_criterion_link"
    __table_args__ = (
        UniqueConstraint(
            "spec_id", "criterion_ext_id", name="uq_traceability_criterion_link_spec_criterion"
        ),
        Index("ix_traceability_criterion_link_project_status", "project_id", "status"),
        Index("ix_traceability_criterion_link_project", "project_id"),
        Index("ix_traceability_criterion_link_spec", "spec_id"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    spec_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("spec_document.id", ondelete="CASCADE"), nullable=False
    )
    spec_key: Mapped[str] = mapped_column(String(64), nullable=False)
    criterion_ext_id: Mapped[str] = mapped_column(String(64), nullable=False)
    criterion_text: Mapped[str] = mapped_column(Text, nullable=False)
    requirement_ext_ids: Mapped[list[Any]] = mapped_column(
        json_type(), default=list, nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    satisfied: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    test_refs: Mapped[list[Any]] = mapped_column(json_type(), default=list, nullable=False)
    diff_refs: Mapped[list[Any]] = mapped_column(json_type(), default=list, nullable=False)
    task_ids: Mapped[list[Any]] = mapped_column(json_type(), default=list, nullable=False)
    pr_numbers: Mapped[list[Any]] = mapped_column(json_type(), default=list, nullable=False)
    report_spec_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_spec_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_validated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TraceabilitySpecRollup(WorkspaceScopedModel):
    """One per-spec rollup projection row (powers the project table)."""

    __tablename__ = "traceability_spec_rollup"
    __table_args__ = (
        UniqueConstraint("spec_id", name="uq_traceability_spec_rollup_spec"),
        Index("ix_traceability_spec_rollup_project_status", "project_id", "validation_status"),
        Index("ix_traceability_spec_rollup_project", "project_id"),
        Index("ix_traceability_spec_rollup_epic", "epic_id"),
    )

    spec_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("spec_document.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    spec_key: Mapped[str] = mapped_column(String(64), nullable=False)
    spec_name: Mapped[str] = mapped_column(String(512), nullable=False)
    epic_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    spec_status: Mapped[str] = mapped_column(String(32), nullable=False)
    total_requirements: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    covered_requirements: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_criteria: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    validated_criteria: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_criteria: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    uncovered_criteria: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    claimed_criteria: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    stale_criteria: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    requirement_coverage: Mapped[float] = mapped_column(Numeric(5, 4), default=0, nullable=False)
    acceptance_criteria_coverage: Mapped[float] = mapped_column(
        Numeric(5, 4), default=0, nullable=False
    )
    uncovered_requirement_ext_ids: Mapped[list[Any]] = mapped_column(
        json_type(), default=list, nullable=False
    )
    validation_status: Mapped[str] = mapped_column(String(16), default="none", nullable=False)
    gap_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_validated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    projection_version: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = ["TraceabilityCriterionLink", "TraceabilitySpecRollup"]
