"""The shadow governance gate: what automation *would* decide.

Pure module — no I/O. The gate never applies anything; its verdict is stored
on each proposal so its agreement with human decisions can be measured
before any enforcement is considered. Only human decisions count as
precedent.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

Verdict = Literal["accept", "reject", "unsure"]
GateOutcome = Literal["auto", "hold"]


@dataclass(frozen=True)
class Precedent:
    """One past decision on a comparable proposal."""

    human: bool
    decision: Literal["accept", "reject"]


@dataclass(frozen=True)
class GateDecision:
    """The gate's shadow verdict, stored verbatim on the proposal."""

    decision: GateOutcome
    threshold: float | None
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        """The JSON shape stored on ``world_proposal.gate``."""
        return {
            "decision": self.decision,
            "threshold": self.threshold,
            "reasons": list(self.reasons),
        }


def gate(
    verdict: Verdict | None,
    confidence: float | None,
    *,
    type_conflict: bool,
    precedents: Sequence[Precedent] = (),
) -> GateDecision:
    """Decide ``auto`` vs ``hold`` for one proposal, in shadow.

    A type conflict or a contradicting human precedent always holds. The
    confidence threshold is 0.75 when a human precedent agrees with the
    verdict, else 0.85. Only a decisive verdict (``accept``/``reject``) at
    or above the threshold would auto-apply; everything else holds for a
    person.
    """
    human = [p for p in precedents if p.human]
    if type_conflict:
        return GateDecision("hold", None, ("type conflict with the declared ontology",))

    decisive: Literal["accept", "reject"] | None = (
        verdict if verdict in ("accept", "reject") else None
    )
    if decisive is not None and any(p.decision != decisive for p in human):
        return GateDecision("hold", None, ("a human precedent contradicts this verdict",))

    agrees = decisive is not None and any(p.decision == decisive for p in human)
    threshold = 0.75 if agrees else 0.85
    if decisive is None:
        return GateDecision("hold", threshold, ("no decisive verdict to evaluate",))
    if confidence is None or confidence < threshold:
        shown = "none" if confidence is None else f"{confidence:.2f}"
        return GateDecision(
            "hold", threshold, (f"confidence {shown} is below the {threshold:.2f} threshold",)
        )
    reasons = [f"decisive {decisive} verdict at confidence {confidence:.2f}"]
    if agrees:
        reasons.append("a human precedent agrees (threshold 0.75)")
    return GateDecision("auto", threshold, tuple(reasons))


__all__ = ["GateDecision", "GateOutcome", "Precedent", "Verdict", "gate"]
