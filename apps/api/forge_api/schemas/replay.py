"""Request/response schemas for Time-Travel Runs replay
(``POST /agent/runs/{run_id}/replay``).
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field

from forge_contracts import AgentObjective, AgentRunResult

__all__ = [
    "ReplayCallDiffOut",
    "ReplayDivergenceOut",
    "ReplayRunRequest",
    "ReplayRunResponse",
]


class ReplayRunRequest(BaseModel):
    """The objective that produced the recording being replayed.

    A ``RunRecording`` cassette holds only the two nondeterministic
    boundaries (LLM completions + tool dispatches) and a redacted env
    snapshot — not the ``AgentObjective`` that drove the run (it is not part
    of what ``forge_worker.agent_runner`` tapes). The caller supplies the
    same objective so the runtime regenerates the identical request stream;
    replay then checks every call against the tape by substitution.
    """

    objective: AgentObjective


class ReplayCallDiffOut(BaseModel):
    """One recorded call (an LLM completion or a tool dispatch) vs. its replay."""

    boundary: Literal["llm", "tool"]
    index: int
    #: The tool name (``boundary == "tool"`` only); ``None`` for an LLM call.
    name: str | None = None
    matched: bool
    recorded_digest: str | None = None
    #: ``None`` when the replay never reached this call (it stopped earlier,
    #: either because it diverged first or because it finished the run
    #: before consuming the whole tape).
    replay_digest: str | None = None


class ReplayDivergenceOut(BaseModel):
    """Where + why the replay first drifted from the tape (``None`` if it did not)."""

    boundary: Literal["llm", "tool"]
    index: int
    name: str | None = None
    expected: str | None = None
    actual: str


class ReplayRunResponse(BaseModel):
    """The outcome of replaying one ``RunRecording`` — a step-by-step diff."""

    run_recording_id: uuid.UUID
    #: True iff the replay drifted from the tape: either a call's request/args
    #: digest diverged (see ``divergence``), or the replay finished having
    #: consumed fewer calls than were recorded.
    diverged: bool
    divergence: ReplayDivergenceOut | None = None
    steps: list[ReplayCallDiffOut] = Field(default_factory=list)
    #: The replayed run's result — ``None`` when the replay diverged (a
    #: divergence aborts the run; there is no result to return).
    result: AgentRunResult | None = None
