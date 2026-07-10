"""Deep-analytics backends (F40-OBS-ANALYTICS): the pg-testable ceiling below
live Grafana/OTLP dashboards.

* :mod:`forge_obs.analytics.incidents` — MTTA/MTTR/remediation accept rate.
* :mod:`forge_obs.analytics.dora` — deploy frequency, lead time, change-failure
  rate, MTTR from deploy events.
* :mod:`forge_obs.analytics.fx` + :mod:`forge_obs.analytics.budgets` — spend
  budgets with hard-cap alerts, multi-currency via an FX rate table.
* :mod:`forge_obs.analytics.skill_snapshot` — immutable per-run skill-profile
  snapshot.
* :mod:`forge_obs.analytics.coverage` — coverage-over-time snapshot, recompute,
  and org rollup.
"""

from __future__ import annotations

from forge_obs.analytics.budgets import Budget, BudgetStatus, SqlBudgetReader, evaluate_budget
from forge_obs.analytics.coverage import (
    CoverageSnapshotDTO,
    CoverageTrendPoint,
    SqlCoverageRepository,
    compute_coverage_pct,
)
from forge_obs.analytics.dora import DoraMetrics, SqlDoraReader, compute_dora_metrics
from forge_obs.analytics.fx import DbFxRateBook, FxRate, FxRateBook, InMemoryFxRateBook, convert
from forge_obs.analytics.incidents import (
    IncidentReliabilityMetrics,
    SqlIncidentReliabilityReader,
    compute_incident_reliability,
)
from forge_obs.analytics.skill_snapshot import (
    SkillProfileSnapshotDTO,
    SqlSkillProfileSnapshotRepository,
    build_directives_payload,
)

__all__ = [
    "Budget",
    "BudgetStatus",
    "CoverageSnapshotDTO",
    "CoverageTrendPoint",
    "DbFxRateBook",
    "DoraMetrics",
    "FxRate",
    "FxRateBook",
    "InMemoryFxRateBook",
    "IncidentReliabilityMetrics",
    "SkillProfileSnapshotDTO",
    "SqlBudgetReader",
    "SqlCoverageRepository",
    "SqlDoraReader",
    "SqlIncidentReliabilityReader",
    "SqlSkillProfileSnapshotRepository",
    "build_directives_payload",
    "compute_coverage_pct",
    "compute_dora_metrics",
    "compute_incident_reliability",
    "convert",
    "evaluate_budget",
]
