"""Engine-agnostic Red-Team Gate verdict building + recording (V1/V2 parity).

The Red-Team Gate's verdict shape was born inside the Temporal (V2) activity
layer (``temporal/activities.py``); this module EXTRACTS it so the V1
(Postgres-FSM) path mints the identical contract instead of copying it:

* :class:`RedTeamInput` / :class:`RedTeamResult` + the two verdict constants —
  the pure gate payloads, moved here from ``temporal/payloads.py`` (which
  re-exports them unchanged, so every Temporal import path is untouched);
* :func:`parked_pass_verdict` — the explicit park-don't-fake default: with no
  adversary model/sandbox wired the verdict is ``survived``/``kind="parked"``
  with evidence saying so plainly. It is **never** disguised as a real
  adversarial pass;
* :func:`evaluate_red_team` — run the configured adversary when one is wired,
  the parked-pass otherwise (the Temporal activity's ``_default_red_team``
  delegates here);
* :func:`ensure_red_team_verdict` / :func:`run_and_record_red_team` — the
  session-in recorders the V1 gate path and the trigger endpoint call: evaluate,
  then append the verdict to the append-only ``red_team_record`` table via
  :func:`forge_db.redteam.record_red_team_verdict` (which chains the
  ``redteam.survived`` audit event on a survive).

Module-level imports stay pure (dataclasses only): ``temporal/payloads.py``
imports this module inside the workflow determinism sandbox, so the DB pieces
are imported lazily inside the recorder functions (the ``store.py`` precedent).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from forge_db.models.red_team import RedTeamRecord

__all__ = [
    "REDTEAM_BLOCKED",
    "REDTEAM_SURVIVED",
    "RedTeamFn",
    "RedTeamInput",
    "RedTeamResult",
    "ensure_red_team_verdict",
    "evaluate_red_team",
    "parked_pass_verdict",
    "run_and_record_red_team",
]

#: The two terminal verdicts of the Red-Team Gate (mirror
#: ``forge_db.models.red_team.VERDICT_*`` without importing the DB into the
#: workflow determinism sandbox).
REDTEAM_BLOCKED = "blocked"
REDTEAM_SURVIVED = "survived"


@dataclass
class RedTeamInput:
    """Argument to a red-team scan (the ``forge.run_red_team_scan`` activity's
    payload, shared verbatim by the V1 gate path).

    The adversary attacks the candidate spec/diff for ``workflow_run_id`` before
    the human implementation gate; ``coder_model`` is the model that produced the
    change, so the adversary can be routed onto a HETEROGENEOUS one.
    """

    workflow_run_id: uuid.UUID
    workspace_id: uuid.UUID
    task_id: uuid.UUID
    phase: str = "spec"  # the gate the scan runs before (spec | pr)
    coder_model: str | None = None
    idempotency_key: str = ""


@dataclass
class RedTeamResult:
    """Verdict of an adversarial scan.

    ``verdict`` is :data:`REDTEAM_BLOCKED` (the adversary produced a failing test
    or a structured spec-violation) or :data:`REDTEAM_SURVIVED` (it could not).
    ``kind`` names the attack class (``failing_test`` / ``spec_violation`` /
    ``parked``); ``evidence`` carries the structured result. Both models are
    recorded so the survive is a truthful, heterogeneous-review provenance fact.
    """

    verdict: str = REDTEAM_SURVIVED
    kind: str = "parked"
    evidence: dict[str, Any] = field(default_factory=dict)
    adversary_model: str | None = None
    coder_model: str | None = None

    @property
    def blocked(self) -> bool:
        return self.verdict == REDTEAM_BLOCKED


#: An adversary harness: scan the candidate for ``RedTeamInput`` and return the
#: verdict (real wiring runs the heterogeneous, sandboxed ``run_red_team``).
RedTeamFn = Callable[[RedTeamInput], RedTeamResult]


def parked_pass_verdict() -> RedTeamResult:
    """The explicit park-don't-fake verdict used when no adversary is wired.

    The evidence states plainly that no adversary model/sandbox was configured —
    a ``survived``/``parked`` record must never be read as "an adversary tried
    and failed to break this".
    """
    return RedTeamResult(
        verdict=REDTEAM_SURVIVED,
        kind="parked",
        evidence={"parked": True, "reason": "no adversary model/sandbox wired"},
    )


def evaluate_red_team(inp: RedTeamInput, red_team_fn: RedTeamFn | None = None) -> RedTeamResult:
    """Build the gate verdict: the configured adversary when wired, the explicit
    parked-pass otherwise. Both engine spines call exactly this."""
    if red_team_fn is not None:
        return red_team_fn(inp)
    return parked_pass_verdict()


def run_and_record_red_team(
    session: Session,
    workspace_id: uuid.UUID,
    *,
    workflow_run_id: uuid.UUID,
    task_id: uuid.UUID,
    phase: str = "spec",
    coder_model: str | None = None,
    red_team_fn: RedTeamFn | None = None,
    actor_id: uuid.UUID | None = None,
) -> RedTeamRecord:
    """Evaluate the gate and APPEND the verdict row (+ ``redteam.survived``
    audit event on a survive) on the caller's transaction.

    Always records a fresh scan — the explicit-trigger semantics (a ``blocked``
    scan followed by a re-triggered ``survived`` one builds the history the
    read surface documents). The caller commits.
    """
    from forge_db.redteam import record_red_team_verdict

    result = evaluate_red_team(
        RedTeamInput(
            workflow_run_id=workflow_run_id,
            workspace_id=workspace_id,
            task_id=task_id,
            phase=phase,
            coder_model=coder_model,
            idempotency_key=f"{workflow_run_id}:red_team:{phase}",
        ),
        red_team_fn,
    )
    row, _ = record_red_team_verdict(
        session,
        workspace_id,
        verdict=result.verdict,
        kind=result.kind,
        evidence=result.evidence,
        adversary_model=result.adversary_model,
        coder_model=result.coder_model,
        workflow_run_id=workflow_run_id,
        actor_id=actor_id,
    )
    return row


def ensure_red_team_verdict(
    session: Session,
    workspace_id: uuid.UUID,
    *,
    workflow_run_id: uuid.UUID,
    task_id: uuid.UUID,
    phase: str = "spec",
    coder_model: str | None = None,
    red_team_fn: RedTeamFn | None = None,
    actor_id: uuid.UUID | None = None,
) -> tuple[RedTeamRecord, bool]:
    """Idempotently mint the gate verdict for a run: evaluate + record only when
    the run has no scan yet; otherwise return the newest existing record.

    Mirrors the Temporal spine's once-per-run scan (a block -> changes ->
    resubmit loop re-enters the gate without rescanning). Returns
    ``(record, created)``. The caller commits.
    """
    from forge_db.redteam import RedTeamRepository

    # Query-first idempotency is best-effort, not a hard guarantee: two
    # concurrent callers can both see "no existing scan" and both record.
    # Worst case under a race is a duplicate honest `parked`/`survived` row in
    # the append-only history (extra noise on the GET's `records` list) — it
    # is never a verdict flip, and never fabricates a block or a survive.
    existing = RedTeamRepository(session).get_by_run(workspace_id, workflow_run_id)
    if existing:
        return existing[0], False
    row = run_and_record_red_team(
        session,
        workspace_id,
        workflow_run_id=workflow_run_id,
        task_id=task_id,
        phase=phase,
        coder_model=coder_model,
        red_team_fn=red_team_fn,
        actor_id=actor_id,
    )
    return row, True
