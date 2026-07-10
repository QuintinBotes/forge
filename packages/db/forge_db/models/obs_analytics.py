"""F40-OBS-ANALYTICS models: skill snapshots, coverage trend, budgets, FX rates.

Four self-contained tables extending the observability domain (``forge_obs``
reads/writes them; nothing existing is altered):

* ``skill_profile_snapshot`` — an immutable, one-row-per-run record of the
  resolved :class:`~forge_contracts.SkillProfile` directives an ``agent_run``
  executed under (the "F04 ``repo_policy_snapshot``" precedent referenced by
  :class:`~forge_db.models.policy_rule_evaluation.PolicyRuleEvaluation` does not
  exist in this foundation, so this is the first concrete per-run immutable
  snapshot table; it follows the same append-only convention). Hardened via the
  shared :func:`attach_immutability_trigger` (Postgres BEFORE UPDATE/DELETE
  block; a no-op on SQLite, where the repository exposes no update/delete path).
* ``coverage_snapshot`` — derived per-repo-per-day test-coverage rollup, mirroring
  ``sprint_burndown_snapshot``: idempotent daily upsert keyed by
  ``(project_id, repo_id, snapshot_date)``, fully rebuildable from CI coverage
  reports by the recompute job (``forge_obs.analytics.coverage``). Not
  append-only — a same-day recompute corrects the row.
* ``budget`` — a workspace/project spend cap (period + amount + currency +
  ``hard_cap``), read by the budget-alert evaluator
  (``forge_obs.analytics.budgets``).
* ``fx_rate`` — an effective-dated currency-conversion price book (mirrors
  ``model_price``'s resolution rule: newest ``effective_from <= at``), letting a
  budget denominated in a non-USD currency compare against the USD-denominated
  ``cost_event`` ledger.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from forge_db.base import (
    ForgeModel,
    WorkspaceScopedModel,
    attach_immutability_trigger,
    enum_type,
    json_type,
)
from forge_db.models.enums import BudgetPeriod, BudgetScope

__all__ = ["Budget", "CoverageSnapshot", "FxRate", "SkillProfileSnapshot"]


class SkillProfileSnapshot(WorkspaceScopedModel):
    """Immutable per-run capture of the skill profile directives in force."""

    __tablename__ = "skill_profile_snapshot"
    __table_args__ = (
        UniqueConstraint("agent_run_id", name="uq_skill_profile_snapshot_agent_run_id"),
        Index("ix_skill_profile_snapshot_workspace_captured", "workspace_id", "captured_at"),
    )

    agent_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_run.id", ondelete="CASCADE"), nullable=False
    )
    profile_name: Mapped[str] = mapped_column(String(128), nullable=False)
    min_test_coverage: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Allowed/forbidden actions are frozensets in-memory but serialize as
    # sorted lists (see ``build_directives_payload``), so the row round-trips
    # through plain ``dict``/``list`` JSON.
    directives: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class CoverageSnapshot(WorkspaceScopedModel):
    """Derived per-repo-per-day test-coverage rollup (rebuildable, upsertable)."""

    __tablename__ = "coverage_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "repo_id",
            "snapshot_date",
            name="uq_coverage_snapshot_project_repo_date",
        ),
        Index("ix_coverage_snapshot_project_date", "project_id", "snapshot_date"),
        Index("ix_coverage_snapshot_workspace_date", "workspace_id", "snapshot_date"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    repo_id: Mapped[str] = mapped_column(String(512), nullable=False)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    lines_covered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lines_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    coverage_pct: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, default=0)


class Budget(WorkspaceScopedModel):
    """A recurring spend cap over a workspace or project scope."""

    __tablename__ = "budget"
    __table_args__ = (Index("ix_budget_workspace_scope", "workspace_id", "scope", "project_id"),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    scope: Mapped[BudgetScope] = mapped_column(
        enum_type(BudgetScope), default=BudgetScope.WORKSPACE, nullable=False
    )
    # NULL unless scope == "project" (checked by the evaluator, not the schema —
    # SQLite's create_all path has no portable CHECK-vs-enum-column story).
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("project.id", ondelete="CASCADE"), nullable=True
    )
    period: Mapped[BudgetPeriod] = mapped_column(
        enum_type(BudgetPeriod), default=BudgetPeriod.MONTHLY, nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(8), default="USD", server_default=text("'USD'"), nullable=False
    )
    hard_cap: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )


class FxRate(ForgeModel):
    """Effective-dated currency-conversion rate (global; not tenant-scoped)."""

    __tablename__ = "fx_rate"
    __table_args__ = (
        Index("ix_fx_rate_lookup", "base_currency", "quote_currency", "effective_from"),
    )

    base_currency: Mapped[str] = mapped_column(String(8), nullable=False)
    quote_currency: Mapped[str] = mapped_column(String(8), nullable=False)
    #: 1 unit of ``base_currency`` == ``rate`` units of ``quote_currency``.
    rate: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# Append-only enforcement on Postgres (no-op on SQLite; the repository exposes
# no update/delete path there).
attach_immutability_trigger(SkillProfileSnapshot.__table__)
