"""Deterministic task/spec complexity + seniority-tier sizing.

Adaptive Orchestration policy: score a task/spec from its signals (kind,
priority, blast radius, file/repo footprint, requirement + acceptance-criteria
counts, whether it touches contracts/security, dependency count, and
ambiguity) into a ``{tier, strategy, reasons}`` sizing decision.

This module is pure and fully deterministic: the same :class:`SizingSignals`
always produce the same :class:`ComplexitySizing`. It does not call a model,
inspect a database, or otherwise perform I/O — later Adaptive Orchestration
slices (the model router, per-role model+effort config) consume the ``tier``
and ``strategy`` this module produces.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from forge_contracts import Priority, SpecManifest, TaskKind

Tier = Literal["junior", "medior", "senior"]
Strategy = Literal["single", "swarm"]
BlastRadiusLevel = Literal["low", "medium", "high"]

#: Score thresholds separating tiers (inclusive upper bounds for junior/medior;
#: anything above ``_MEDIOR_MAX`` is senior). Kept private: callers consume
#: ``tier``, never the raw score, so these can be retuned without breaking API.
_JUNIOR_MAX = 6
_MEDIOR_MAX = 16

_KIND_POINTS: dict[TaskKind, int] = {
    TaskKind.DOC: 0,
    TaskKind.CHORE: 0,
    TaskKind.BUG: 1,
    TaskKind.SPIKE: 1,
    TaskKind.FEATURE: 1,
    TaskKind.CHANGE_REQUEST: 2,
    TaskKind.INCIDENT: 3,
}

_PRIORITY_POINTS: dict[Priority, int] = {
    Priority.LOW: 0,
    Priority.MEDIUM: 1,
    Priority.HIGH: 2,
    Priority.URGENT: 3,
}

_BLAST_RADIUS_POINTS: dict[BlastRadiusLevel, int] = {
    "low": 0,
    "medium": 2,
    "high": 4,
}


class SizingSignals(BaseModel):
    """Normalized inputs to :func:`score_complexity` (spec: ao-complexity signals).

    All fields default to the smallest/least-complex value so a caller may
    supply only the signals it has and still get a deterministic (junior,
    single) result for a trivial/empty task.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    kind: TaskKind = TaskKind.FEATURE
    priority: Priority = Priority.MEDIUM
    #: ``None`` when the task/spec has no assessed blast radius (scores as 0,
    #: same as "low").
    blast_radius: BlastRadiusLevel | None = None
    file_count: int = 0
    repo_count: int = 1
    requirement_count: int = 0
    acceptance_criteria_count: int = 0
    touches_contracts: bool = False
    touches_security: bool = False
    dependency_count: int = 0
    open_questions_count: int = 0
    #: Explicit ambiguity/underspecified signal (e.g. requirements present but
    #: no acceptance criteria yet, or a human/AI reviewer flagged the spec as
    #: not yet actionable). Distinct from ``open_questions_count`` so a caller
    #: can flag ambiguity even when no ``OpenQuestion`` rows exist yet.
    underspecified: bool = False


class ComplexitySizing(BaseModel):
    """The deterministic sizing decision (spec: ``{tier, strategy, reasons[]}``)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    tier: Tier
    strategy: Strategy
    score: int
    reasons: list[str] = Field(default_factory=list)


def _bucket(value: int, thresholds: list[tuple[int, int]]) -> int:
    """Return the points for the first ``(max_inclusive, points)`` bucket ``value`` fits."""
    for max_inclusive, points in thresholds:
        if value <= max_inclusive:
            return points
    return thresholds[-1][1]


def signals_from_spec(manifest: SpecManifest, **overrides: object) -> SizingSignals:
    """Build :class:`SizingSignals` from a :class:`~forge_contracts.SpecManifest`.

    Derives ``repo_count``/``requirement_count``/``acceptance_criteria_count``/
    ``open_questions_count`` from the manifest, and flags ``underspecified``
    when requirements exist but acceptance criteria do not (a spec that has
    not yet been fully fleshed out). Any task-level signal the manifest does
    not carry (``kind``, ``priority``, ``blast_radius``, ``file_count``,
    ``touches_contracts``, ``touches_security``, ``dependency_count``) may be
    supplied via ``overrides`` and takes precedence over the manifest-derived
    defaults.
    """
    derived: dict[str, object] = {
        "repo_count": max(len(manifest.repos), 1),
        "requirement_count": len(manifest.requirements),
        "acceptance_criteria_count": len(manifest.acceptance_criteria),
        "open_questions_count": len(manifest.open_questions),
        "underspecified": bool(manifest.requirements) and not manifest.acceptance_criteria,
    }
    derived.update(overrides)
    return SizingSignals.model_validate(derived)


def score_complexity(signals: SizingSignals) -> ComplexitySizing:
    """Score ``signals`` into a deterministic ``{tier, strategy, reasons}`` sizing."""
    score = 0
    reasons: list[str] = []

    kind_points = _KIND_POINTS.get(signals.kind, 1)
    if kind_points:
        score += kind_points
        reasons.append(f"kind={signals.kind.value} (+{kind_points})")

    priority_points = _PRIORITY_POINTS.get(signals.priority, 1)
    if priority_points:
        score += priority_points
        reasons.append(f"priority={signals.priority.value} (+{priority_points})")

    if signals.blast_radius is not None:
        blast_points = _BLAST_RADIUS_POINTS.get(signals.blast_radius, 0)
        if blast_points:
            score += blast_points
            reasons.append(f"blast_radius={signals.blast_radius} (+{blast_points})")

    files_points = _bucket(signals.file_count, [(2, 0), (5, 1), (15, 2), (10**9, 4)])
    if files_points:
        score += files_points
        reasons.append(f"file_count={signals.file_count} (+{files_points})")

    if signals.repo_count > 2:
        repo_points = 4
    elif signals.repo_count == 2:
        repo_points = 2
    else:
        repo_points = 0
    if repo_points:
        score += repo_points
        reasons.append(f"repo_count={signals.repo_count} (+{repo_points})")

    req_points = _bucket(signals.requirement_count, [(3, 0), (8, 1), (10**9, 3)])
    if req_points:
        score += req_points
        reasons.append(f"requirement_count={signals.requirement_count} (+{req_points})")

    ac_points = _bucket(signals.acceptance_criteria_count, [(3, 0), (8, 1), (10**9, 3)])
    if ac_points:
        score += ac_points
        reasons.append(
            f"acceptance_criteria_count={signals.acceptance_criteria_count} (+{ac_points})"
        )

    if signals.touches_contracts:
        score += 4
        reasons.append("touches_contracts (+4)")

    if signals.touches_security:
        score += 4
        reasons.append("touches_security (+4)")

    dep_points = _bucket(signals.dependency_count, [(0, 0), (2, 1), (10**9, 3)])
    if dep_points:
        score += dep_points
        reasons.append(f"dependency_count={signals.dependency_count} (+{dep_points})")

    oq_points = _bucket(signals.open_questions_count, [(0, 0), (2, 2), (10**9, 4)])
    if oq_points:
        score += oq_points
        reasons.append(f"open_questions_count={signals.open_questions_count} (+{oq_points})")

    if signals.underspecified:
        score += 3
        reasons.append("underspecified (+3)")

    if score <= _JUNIOR_MAX:
        tier: Tier = "junior"
    elif score <= _MEDIOR_MAX:
        tier = "medior"
    else:
        tier = "senior"
    reasons.append(f"score={score} -> tier={tier}")

    strategy: Strategy = "single"
    strategy_reasons: list[str] = []
    if tier == "senior":
        strategy = "swarm"
        strategy_reasons.append("tier=senior")
    if signals.repo_count > 1:
        strategy = "swarm"
        strategy_reasons.append(f"repo_count={signals.repo_count}>1")
    if signals.touches_contracts and signals.touches_security:
        strategy = "swarm"
        strategy_reasons.append("touches_contracts and touches_security")

    if strategy == "swarm":
        reasons.append(f"strategy=swarm ({', '.join(strategy_reasons)})")
    else:
        reasons.append("strategy=single (no swarm trigger)")

    return ComplexitySizing(tier=tier, strategy=strategy, score=score, reasons=reasons)
