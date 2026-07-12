"""Request/response schemas for a Time-Travel Runs counterfactual fork
(``POST /agent/runs/{run_id}/fork``).

A fork replays a recorded run up to ``fork_index`` and then lets it diverge on
purpose against a **different** model (and, optionally, an augmented prompt) —
"what if the agent had switched models at step N?". The pre-fork prefix is
still checked against the tape by substitution, so a fork whose objective does
not reproduce the recording up to the fork point reports a divergence exactly
like a full replay.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from forge_api.schemas.replay import ReplayDivergenceOut
from forge_contracts import AgentObjective, AgentRunResult

__all__ = ["ForkRunRequest", "ForkRunResponse"]


class ForkRunRequest(BaseModel):
    """Fork a recording at ``fork_index`` onto ``model`` (+ optional prompt).

    ``objective`` is the same objective that produced the recording (as for
    replay — the cassette does not store it), so the runtime regenerates the
    identical request stream for the replayed prefix. ``model`` is the new
    model the post-fork completions run against, built via the per-role
    ``model_client_factory`` seam. ``prompt_override``, when supplied, is
    appended to the system prompt of every post-fork completion.
    """

    objective: AgentObjective
    #: The call-index at which to stop replaying and start running live. ``0``
    #: forks from the very first call (a fully counterfactual re-run).
    fork_index: int = Field(default=0, ge=0)
    #: The (different) model the post-fork completions run against.
    model: str = Field(min_length=1)
    #: Optional extra system-prompt text applied to post-fork completions only.
    prompt_override: str | None = None


class ForkRunResponse(BaseModel):
    """The outcome of a counterfactual fork — the diverged run's result."""

    run_recording_id: uuid.UUID
    fork_index: int
    model: str
    #: True iff the *pre-fork* prefix drifted from the tape (the objective did
    #: not reproduce the recording up to ``fork_index``); the fork aborts and
    #: ``result`` is ``None``. Post-fork divergence is expected and is *not*
    #: reported here — that is the whole point of a fork.
    diverged: bool
    divergence: ReplayDivergenceOut | None = None
    #: The counterfactual run's result — ``None`` when the pre-fork prefix
    #: diverged (there is no run to return).
    result: AgentRunResult | None = None
