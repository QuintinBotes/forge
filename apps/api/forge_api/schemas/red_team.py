"""Response schema for the Red-Team Gate surface
(``GET /workflow/runs/{run_id}/red-team``, Red-Team Gate, slice redteam-surface).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["RedTeamGateOut", "RedTeamRecordOut"]


class RedTeamRecordOut(BaseModel):
    """One immutable adversarial-review verdict (mirrors ``forge_db.models.RedTeamRecord``)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    #: ``blocked`` or ``survived`` — see ``forge_db.models.red_team.VERDICT_*``.
    verdict: str
    #: The attack class that produced the verdict (``failing_test``,
    #: ``spec_violation``, or ``parked``).
    kind: str
    #: The structured attack result — failing test output, spec-violation
    #: payload, or the parked reason.
    evidence: dict[str, Any] = Field(default_factory=dict)
    adversary_model: str | None = None
    coder_model: str | None = None
    created_at: datetime


class RedTeamGateOut(BaseModel):
    """The Red-Team Gate surface for one workflow run: latest verdict + history.

    ``latest`` is the most recent scan (newest ``created_at`` first) — what the
    human approval gate's badge shows. ``records`` carries the full scan
    history (a ``blocked`` scan followed by a re-submitted ``survived`` one is
    common). ``latest is None`` means no scan has been recorded for this run
    yet (or the caller does not own it) — both read identically so the
    endpoint never leaks cross-tenant existence.
    """

    workflow_run_id: uuid.UUID
    latest: RedTeamRecordOut | None = None
    records: list[RedTeamRecordOut] = Field(default_factory=list)
