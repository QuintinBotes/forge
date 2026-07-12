"""``RedTeamRecord`` — the append-only verdict of the Red-Team Gate.

(red-team-gate) The storage substrate backing "survived adversarial review":
one immutable row per adversarial scan of a candidate spec/diff, recorded
*before* the change reaches the human implementation gate. A distinct ADVERSARY
agent — running a HETEROGENEOUS model (a different provider/tier than the coder
used) — attacks the candidate and either BLOCKS it (produced a failing
executable test or a structured spec-violation) or fails to, in which case the
change earns a ``survived`` record that feeds the Phase-1 attestation.

Both models are captured for provenance: ``adversary_model`` (what attacked)
must differ from ``coder_model`` (what produced the change) — the heterogeneity
that makes the review meaningful — and ``evidence`` holds the structured attack
result (the failing test's output, the spec-violation, or the parked reason).

Append-only, mirroring ``attestation`` / ``run_recording``: the repository
exposes no update/delete path, and the table opts into the shared Postgres
``attach_immutability_trigger`` (BEFORE UPDATE/DELETE -> raise; a no-op on the
SQLite unit path, where the missing mutation surface is the only guard).

``workflow_run_id`` is a nullable ``CASCADE`` FK mirroring ``Attestation`` /
``RunRecording`` exactly (see ``Attestation``'s docstring for the accepted
CASCADE-vs-immutability-trigger tension): a scan is normally attached to the
run it gated, but may be recorded before the run is linked.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from forge_db.base import WorkspaceScopedModel, attach_immutability_trigger, json_type

#: The two terminal verdicts of a red-team scan.
VERDICT_BLOCKED = "blocked"
VERDICT_SURVIVED = "survived"


class RedTeamRecord(WorkspaceScopedModel):
    """One immutable adversarial-review verdict over a candidate change."""

    __tablename__ = "red_team_record"
    __table_args__ = (
        Index("ix_red_team_record_workspace_created", "workspace_id", "created_at"),
        Index("ix_red_team_record_workflow_run_id", "workflow_run_id"),
        Index("ix_red_team_record_verdict", "workspace_id", "verdict"),
    )

    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workflow_run.id", ondelete="CASCADE"), nullable=True
    )
    #: ``blocked`` (the adversary produced a failing test / spec-violation) or
    #: ``survived`` (it could not) — the two values of :data:`VERDICT_BLOCKED` /
    #: :data:`VERDICT_SURVIVED`.
    verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    #: The attack class that produced the verdict (e.g. ``failing_test``,
    #: ``spec_violation``, or ``parked`` when no adversary/sandbox was wired).
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The structured attack result — the failing test's output, the
    #: spec-violation payload, or the parked reason.
    evidence: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict, nullable=False)
    #: The model the ADVERSARY ran under (heterogeneous — must differ from
    #: ``coder_model``). Nullable: a parked-pass has no adversary model.
    adversary_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: The model the change under review was produced by (the coder).
    coder_model: Mapped[str | None] = mapped_column(String(128), nullable=True)


# Postgres: append-only via the shared BEFORE UPDATE/DELETE trigger.
attach_immutability_trigger(RedTeamRecord.__table__)


__all__ = ["VERDICT_BLOCKED", "VERDICT_SURVIVED", "RedTeamRecord"]
