"""Run-trace assembler for the step-level trace viewer (Task 1.14).

Spec Observability: "Run trace viewer with step-level inspection" and
"Replayable workflow runs with step-level inspection". Approval UI must show
"Full run trace with step-by-step actions taken".

The assembler takes an agent run's recorded :class:`~forge_contracts.Step` list
(optionally with sub-agent steps), orders it deterministically, reindexes it
contiguously, redacts secrets from free-text fields, and summarises it into a
:class:`RunTrace` for the API/UI.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from forge_api.observability.redaction import redact_text
from forge_contracts import AgentRunResult, Step
from forge_contracts.enums import RunStatus


class RunTrace(BaseModel):
    """An ordered, summarised view of a single run's steps."""

    run_id: uuid.UUID | None = None
    status: RunStatus | None = None
    steps: list[Step] = Field(default_factory=list)
    total_steps: int = 0
    step_counts: dict[str, int] = Field(default_factory=dict)
    total_duration_ms: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None
    confidence: float | None = None
    has_subagents: bool = False
    summary: str | None = None


class RunTraceAssembler:
    """Assemble ordered, redacted run traces from recorded steps."""

    def __init__(self, *, redact: bool = True) -> None:
        self._redact = redact

    def assemble(
        self,
        run_id: uuid.UUID | None,
        steps: list[Step],
        *,
        status: RunStatus | None = None,
        confidence: float | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        subagent_steps: dict[str, list[Step]] | None = None,
        summary: str | None = None,
    ) -> RunTrace:
        timeline: list[Step] = list(self._order_steps(list(steps)))

        has_subagents = bool(subagent_steps)
        if subagent_steps:
            for role, sub_steps in subagent_steps.items():
                for step in self._order_steps(list(sub_steps)):
                    timeline.append(
                        step.model_copy(
                            update={"metadata": {**step.metadata, "subagent_role": role}}
                        )
                    )

        processed = [self._process(step, index) for index, step in enumerate(timeline)]

        step_counts: dict[str, int] = {}
        total_duration = 0
        timestamps: list[datetime] = []
        for step in processed:
            step_counts[step.kind.value] = step_counts.get(step.kind.value, 0) + 1
            total_duration += step.duration_ms or 0
            if step.timestamp is not None:
                timestamps.append(step.timestamp)

        return RunTrace(
            run_id=run_id,
            status=status,
            steps=processed,
            total_steps=len(processed),
            step_counts=step_counts,
            total_duration_ms=total_duration,
            started_at=started_at or (min(timestamps) if timestamps else None),
            completed_at=completed_at or (max(timestamps) if timestamps else None),
            confidence=confidence,
            has_subagents=has_subagents,
            summary=summary or self._summarise(len(processed), status),
        )

    def from_agent_result(self, result: AgentRunResult) -> RunTrace:
        """Assemble a trace directly from an :class:`AgentRunResult`."""
        return self.assemble(
            result.run_id,
            list(result.steps),
            status=result.status,
            confidence=result.confidence,
        )

    def from_steps(
        self,
        run_id: uuid.UUID | None,
        raw: list[Step | dict[str, Any]],
        **kwargs: Any,
    ) -> RunTrace:
        """Assemble a trace from a mix of :class:`Step` objects and raw dicts."""
        steps = [s if isinstance(s, Step) else Step.model_validate(s) for s in raw]
        return self.assemble(run_id, steps, **kwargs)

    # -- internals ---------------------------------------------------------- #

    def _order_steps(self, steps: list[Step]) -> list[Step]:
        """Order by explicit index, else by timestamp, else preserve input order."""
        if steps and all(s.index is not None for s in steps):
            return sorted(steps, key=lambda s: s.index or 0)
        if steps and all(s.timestamp is not None for s in steps):
            return sorted(steps, key=lambda s: s.timestamp or datetime.min)
        return list(steps)

    def _process(self, step: Step, index: int) -> Step:
        update: dict[str, Any] = {"index": index}
        if self._redact:
            if step.thought:
                update["thought"] = redact_text(step.thought)
            if step.observation:
                update["observation"] = redact_text(step.observation)
            if step.output:
                update["output"] = redact_text(step.output)
        return step.model_copy(update=update)

    @staticmethod
    def _summarise(count: int, status: RunStatus | None) -> str:
        suffix = f" ({status.value})" if status is not None else ""
        return f"{count} step{'s' if count != 1 else ''}{suffix}"
