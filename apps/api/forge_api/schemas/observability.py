"""Request body for the run-trace producer route (slice RT-7)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from forge_contracts import Step
from forge_contracts.enums import RunStatus


class RecordRunRequest(BaseModel):
    """Body for ``POST /observability/runs/{run_id}/trace`` — record/update a run trace.

    The producer (agent/workflow runtime) posts a run's step trace as it
    progresses; ``status`` drives which ``run.*`` realtime event fans out to the
    workspace's live ``/ws`` sockets (started/updated/completed/failed).
    """

    steps: list[Step] = Field(default_factory=list)
    status: RunStatus | None = None
    confidence: float | None = None


__all__ = ["RecordRunRequest"]
