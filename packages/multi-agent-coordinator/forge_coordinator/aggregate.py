"""Final validation + confidence aggregation -> AgentRunResult (F27 §3.3).

``validate`` checks acceptance criteria against the **merged integration tree**,
not subagent self-agreement (Multi-Agent Rule: "Final acceptance validates against
the approved spec, not subagent agreement"). The aggregate confidence is the min
of the required subagents' confidences, clamped below threshold when a reviewer
rejected — forcing a human interrupt.
"""

from __future__ import annotations

from dataclasses import dataclass

from forge_contracts import (
    AcceptanceCriterion,
    MergeResult,
)

__all__ = ["AcceptanceCheck", "aggregate_confidence", "validate_acceptance"]


@dataclass(frozen=True)
class AcceptanceCheck:
    """Whether one acceptance criterion is satisfied by the merged tree."""

    id: str
    satisfied: bool
    evidence: str | None = None


def validate_acceptance(
    *,
    criteria: list[AcceptanceCriterion],
    merge: MergeResult | None,
    reviewer_ok: bool,
) -> list[AcceptanceCheck]:
    """Map each acceptance criterion -> satisfied against the merged tree.

    A criterion that names an expected path (``spec_ref``) is satisfied only when
    that path appears in the merged changed-file set — even if every subagent
    self-reported success. A criterion with no expected path is satisfied when the
    merged tree has changes and the reviewer (if any) approved.
    """
    changed = list(merge.changed_files) if merge else []
    checks: list[AcceptanceCheck] = []
    for ac in criteria:
        expected = ac.spec_ref
        if expected:
            hit = next((f for f in changed if expected in f), None)
            checks.append(AcceptanceCheck(id=ac.id, satisfied=hit is not None, evidence=hit))
        else:
            satisfied = bool(changed) and reviewer_ok
            checks.append(
                AcceptanceCheck(
                    id=ac.id,
                    satisfied=satisfied,
                    evidence=changed[0] if changed else None,
                )
            )
    return checks


def aggregate_confidence(
    *,
    required_confidences: list[float],
    reviewer_rejected: bool,
    threshold: float,
) -> float:
    """Aggregate confidence = min(required) clamped below threshold on reject."""
    base = min(required_confidences) if required_confidences else 0.0
    if reviewer_rejected:
        base = min(base, threshold - 0.01)
    return round(base, 6)
