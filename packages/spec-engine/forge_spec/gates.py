"""Spec gating rules (FORGE_SPEC: Spec Gating Rules).

The central rule: *no task generation or implementation run without an approved
spec for feature-class work*. Gate violations raise the shared
``SpecGateError`` so the workflow engine / agent runtime can catch one stable
type before starting a run.
"""

from __future__ import annotations

from forge_contracts import SpecGateError, SpecManifest, SpecStatus

#: Statuses from which a spec may generate tasks / be implemented. ``approved``
#: is the human gate; ``implementing``/``validated`` are later lifecycle states
#: that remain implementable (re-runs, fixes).
IMPLEMENTABLE_STATUSES: frozenset[SpecStatus] = frozenset(
    {SpecStatus.APPROVED, SpecStatus.IMPLEMENTING, SpecStatus.VALIDATED}
)


#: Statuses from which a reviewer may still record a reject / request-changes
#: decision. Once a spec is past the human gate (``approved`` and beyond) the
#: review window is closed and the decision endpoints refuse the transition.
REVIEWABLE_STATUSES: frozenset[SpecStatus] = frozenset(
    {
        SpecStatus.DRAFT,
        SpecStatus.CLARIFYING,
        SpecStatus.CHANGES_REQUESTED,
        SpecStatus.REJECTED,
    }
)


def check_implementation_gate(manifest: SpecManifest) -> SpecManifest:
    """Return ``manifest`` if it is implementable; else raise ``SpecGateError``."""
    if manifest.status not in IMPLEMENTABLE_STATUSES:
        raise SpecGateError(
            f"spec {manifest.id!r} is {manifest.status.value!r}; an approved spec "
            f"is required before task generation or implementation "
            f"(allowed: {sorted(s.value for s in IMPLEMENTABLE_STATUSES)})"
        )
    return manifest


def check_review_gate(manifest: SpecManifest) -> SpecManifest:
    """Return ``manifest`` if a review decision may still be recorded.

    Raises the shared ``SpecGateError`` (the same domain-error family the
    approve/tasks gates use) when the spec is already past the human gate —
    e.g. rejecting an ``approved`` spec.
    """
    if manifest.status not in REVIEWABLE_STATUSES:
        raise SpecGateError(
            f"spec {manifest.id!r} is {manifest.status.value!r}; review decisions "
            f"(reject / request changes) are only allowed before the human gate "
            f"(allowed: {sorted(s.value for s in REVIEWABLE_STATUSES)})"
        )
    return manifest


__all__ = [
    "IMPLEMENTABLE_STATUSES",
    "REVIEWABLE_STATUSES",
    "check_implementation_gate",
    "check_review_gate",
]
