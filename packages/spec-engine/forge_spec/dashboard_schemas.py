"""DTOs for the F23 spec-validation dashboard (requirement traceability).

Pydantic v2 transport objects shared by the pure rollup logic
(:mod:`forge_spec.dashboard`), the projector (:mod:`forge_spec.projection`), and
the read service (:mod:`forge_spec.dashboard_service`). No I/O, no DB session.

Foundation deviations from the F23 slice doc (idealised schema conformed to the
real foundation):

* The foundation has no ``CriterionVerdict`` contract and its persisted
  ``ValidationReport`` is *requirement*-grained (``RequirementTrace``), not
  per-criterion. :class:`CriterionVerdict` is therefore introduced **here** as
  the per-acceptance-criterion projection the dashboard needs;
  :func:`forge_spec.dashboard.verdicts_from_report` derives it from the
  requirement-grained report.
* ``EvidenceIndex`` lives here (and is re-exported from ``projection``) so the
  pure ``build_criterion_links`` can consume it without importing the projector.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class CellStatus(StrEnum):
    """Status of a single requirement->criterion traceability cell."""

    UNCOVERED = "uncovered"  # no report references this AC
    CLAIMED = "claimed"  # agent claim but no passing test ref
    VALIDATED = "validated"  # satisfied=true against the current spec version
    FAILED = "failed"  # latest report attempted this AC, satisfied=false
    STALE = "stale"  # was validated, but report_spec_version < current_spec_version


class ValidationStatus(StrEnum):
    """Per-spec rollup verdict."""

    NONE = "none"
    PARTIAL = "partial"
    PASSING = "passing"
    FAILING = "failing"
    STALE = "stale"


class GapKind(StrEnum):
    """The kind of actionable traceability gap."""

    REQUIREMENT_NO_CRITERIA = "requirement_no_criteria"
    CRITERION_NO_TEST = "criterion_no_test"  # claimed, never validated
    CRITERION_FAILED = "criterion_failed"
    CRITERION_STALE = "criterion_stale"


class CriterionVerdict(BaseModel):
    """The per-acceptance-criterion truth derived from a validation report.

    Foundation note: the persisted report is requirement-grained, so this is the
    derived per-AC view (see :func:`forge_spec.dashboard.verdicts_from_report`).
    """

    model_config = ConfigDict(frozen=True)

    criterion_id: str
    satisfied: bool = False
    test_refs: tuple[str, ...] = ()
    rationale: str | None = None


class EvidenceIndex(BaseModel):
    """Per-criterion evidence harvested from F08 PRs/check runs, keyed by AC id.

    The no-op port (pre-F08) returns an empty index (cells show verdict-only
    evidence).
    """

    test_refs: dict[str, list[str]] = Field(default_factory=dict)
    diff_refs: dict[str, list[str]] = Field(default_factory=dict)
    pr_numbers: dict[str, list[int]] = Field(default_factory=dict)
    task_ids: dict[str, list[str]] = Field(default_factory=dict)


class TraceCell(BaseModel):
    """One acceptance-criterion cell in the traceability matrix."""

    criterion_id: str  # "A1"
    criterion_text: str
    requirement_ids: list[str] = Field(default_factory=list)  # ["R1","R3"]
    status: CellStatus
    satisfied: bool = False
    test_refs: list[str] = Field(default_factory=list)
    diff_refs: list[str] = Field(default_factory=list)
    task_ids: list[str] = Field(default_factory=list)
    pr_numbers: list[int] = Field(default_factory=list)
    last_validated_at: datetime | None = None
    report_spec_version: int | None = None


class RequirementTraceRow(BaseModel):
    """One requirement row with its nested acceptance-criterion cells."""

    requirement_id: str  # "R1"
    requirement_text: str
    rollup_status: CellStatus  # worst-of its criteria (uncovered if it has none)
    criteria: list[TraceCell] = Field(default_factory=list)


class SpecTraceabilityMatrix(BaseModel):
    """The full per-spec requirement traceability matrix."""

    spec_id: str
    spec_key: str
    spec_name: str
    spec_status: str
    current_spec_version: int
    requirement_coverage: float
    acceptance_criteria_coverage: float
    validation_status: ValidationStatus
    rows: list[RequirementTraceRow] = Field(default_factory=list)
    last_validated_at: datetime | None = None
    projection_version: int = 0


class SpecValidationRow(BaseModel):
    """One project-table row (one spec)."""

    spec_id: str
    spec_key: str
    spec_name: str
    epic_id: str | None = None
    spec_status: str
    requirement_coverage: float
    acceptance_criteria_coverage: float
    total_requirements: int
    covered_requirements: int
    total_criteria: int
    validated_criteria: int
    failed_criteria: int
    uncovered_criteria: int
    stale_criteria: int
    validation_status: ValidationStatus
    gap_count: int
    last_validated_at: datetime | None = None


class ProjectValidationSummary(BaseModel):
    """Aggregate coverage rollup across every spec in a project."""

    project_id: str
    spec_count: int
    specs_validated: int
    total_requirements: int
    covered_requirements: int
    total_criteria: int
    validated_criteria: int
    requirement_coverage: float
    acceptance_criteria_coverage: float
    open_gap_count: int
    stale_validation_count: int


class TraceabilityGap(BaseModel):
    """A single actionable gap in the dashboard worklist."""

    spec_id: str
    spec_key: str
    kind: GapKind
    requirement_id: str | None = None
    criterion_id: str | None = None
    detail: str  # human-readable, e.g. "A9 validated against v2, current v5"
    deep_link: str  # route to fix it (spec criterion / run trace / PR)


class SpecRollupValues(BaseModel):
    """Computed rollup numbers for one spec (pre-persistence)."""

    total_requirements: int
    covered_requirements: int
    total_criteria: int
    validated_criteria: int
    failed_criteria: int
    uncovered_criteria: int
    claimed_criteria: int
    stale_criteria: int
    requirement_coverage: float
    acceptance_criteria_coverage: float
    uncovered_requirement_ext_ids: list[str] = Field(default_factory=list)
    validation_status: ValidationStatus
    gap_count: int
    last_validated_at: datetime | None = None


class SpecHeader(BaseModel):
    """Identity/scoping header for a spec (resolved from F02 source rows)."""

    spec_id: str
    workspace_id: str
    project_id: str
    spec_key: str
    spec_name: str
    spec_status: str
    epic_id: str | None = None


class CriterionLinkRecord(BaseModel):
    """A row of the ``traceability_criterion_link`` projection (one per AC)."""

    workspace_id: str
    project_id: str
    spec_id: str
    spec_key: str
    criterion_ext_id: str
    criterion_text: str
    requirement_ext_ids: list[str] = Field(default_factory=list)
    status: CellStatus
    satisfied: bool = False
    test_refs: list[str] = Field(default_factory=list)
    diff_refs: list[str] = Field(default_factory=list)
    task_ids: list[str] = Field(default_factory=list)
    pr_numbers: list[int] = Field(default_factory=list)
    report_spec_version: int | None = None
    current_spec_version: int = 1
    last_validated_at: datetime | None = None


class SpecRollupRecord(BaseModel):
    """A row of the ``traceability_spec_rollup`` projection (one per spec)."""

    workspace_id: str
    project_id: str
    spec_id: str
    spec_key: str
    spec_name: str
    epic_id: str | None = None
    spec_status: str
    total_requirements: int
    covered_requirements: int
    total_criteria: int
    validated_criteria: int
    failed_criteria: int
    uncovered_criteria: int
    claimed_criteria: int
    stale_criteria: int
    requirement_coverage: float
    acceptance_criteria_coverage: float
    uncovered_requirement_ext_ids: list[str] = Field(default_factory=list)
    validation_status: ValidationStatus
    gap_count: int
    last_validated_at: datetime | None = None


class Page[T](BaseModel):
    """A cursor-paginated page of rows."""

    items: list[T] = Field(default_factory=list)
    next_cursor: str | None = None
    total: int | None = None


__all__ = [
    "CellStatus",
    "CriterionLinkRecord",
    "CriterionVerdict",
    "EvidenceIndex",
    "GapKind",
    "Page",
    "ProjectValidationSummary",
    "RequirementTraceRow",
    "SpecHeader",
    "SpecRollupRecord",
    "SpecRollupValues",
    "SpecTraceabilityMatrix",
    "SpecValidationRow",
    "TraceCell",
    "TraceabilityGap",
    "ValidationStatus",
]
