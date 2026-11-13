"""The run-event vocabulary (ADR-0013).

Forge emits a named subset of tervin's event vocabulary (re-implemented from its
published names, no code copied). Each event says whether forge **gated** it (its
policy gate, sandboxed tool belt or finalizer decided or performed the action) or
only **observed** it (model text, usage, an outcome). :data:`ENFORCEMENT` is the
only place that flag is set, and :class:`RunEvent` refuses any other value, so an
event can never claim enforcement forge did not have.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from forge_contracts.dtos import Step
from forge_contracts.enums import DecisionEffect, StepKind

__all__ = [
    "ENFORCEMENT",
    "TERMINAL_EVENTS",
    "Enforcement",
    "RunEvent",
    "RunEventDraft",
    "RunEventType",
    "events_for_step",
]

Enforcement = Literal["gated", "observed"]


class RunEventType(StrEnum):
    THREAD_STARTED = "thread.started"
    AGENT_MESSAGE = "agent.message"
    TOOL_REQUESTED = "tool.requested"
    PERMISSION_GRANTED = "permission.granted"
    PERMISSION_DENIED = "permission.denied"
    PERMISSION_REQUESTED = "permission.requested"
    TOOL_COMPLETED = "tool.completed"
    GIT_CHANGED = "git.changed"
    ARTIFACT_CREATED = "artifact.created"
    COST_UPDATED = "cost.updated"
    THREAD_COMPLETED = "thread.completed"
    THREAD_FAILED = "thread.failed"


#: ADR-0013's table: who actually did each kind of event.
ENFORCEMENT: MappingProxyType[RunEventType, Enforcement] = MappingProxyType(
    {
        RunEventType.THREAD_STARTED: "gated",
        RunEventType.AGENT_MESSAGE: "observed",
        RunEventType.TOOL_REQUESTED: "observed",
        RunEventType.PERMISSION_GRANTED: "gated",
        RunEventType.PERMISSION_DENIED: "gated",
        RunEventType.PERMISSION_REQUESTED: "gated",
        RunEventType.TOOL_COMPLETED: "gated",
        RunEventType.GIT_CHANGED: "gated",
        RunEventType.ARTIFACT_CREATED: "gated",
        RunEventType.COST_UPDATED: "observed",
        RunEventType.THREAD_COMPLETED: "observed",
        RunEventType.THREAD_FAILED: "observed",
    }
)

TERMINAL_EVENTS = frozenset({RunEventType.THREAD_COMPLETED, RunEventType.THREAD_FAILED})


class RunEventDraft(BaseModel):
    """An event before forge assigns its run, sequence number and time."""

    model_config = ConfigDict(frozen=True)

    type: RunEventType
    summary: str
    payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def enforcement(self) -> Enforcement:
        return ENFORCEMENT[self.type]


class RunEvent(BaseModel):
    """One event of a run, as stored and as streamed (ADR-0013 envelope)."""

    run_id: uuid.UUID
    seq: int = Field(ge=1)
    type: RunEventType
    ts: datetime
    summary: str
    enforcement: Enforcement
    payload: dict[str, Any] = Field(default_factory=dict)
    # ADR-0015 provenance.
    observed_by: Literal["forge"] = "forge"
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    enforceable: bool = False

    @model_validator(mode="after")
    def _honest(self) -> RunEvent:
        expected = ENFORCEMENT[self.type]
        if self.enforcement != expected:
            raise ValueError(f"{self.type.value} is {expected}, not {self.enforcement}")
        if self.enforceable != (expected == "gated"):
            raise ValueError("enforceable must equal enforcement == 'gated'")
        return self

    @classmethod
    def from_draft(
        cls, draft: RunEventDraft, *, run_id: uuid.UUID, seq: int, ts: datetime
    ) -> RunEvent:
        return cls(
            run_id=run_id,
            seq=seq,
            type=draft.type,
            ts=ts,
            summary=draft.summary,
            enforcement=draft.enforcement,
            payload=draft.payload,
            enforceable=draft.enforcement == "gated",
        )


_PERMISSION_BY_EFFECT = {
    DecisionEffect.ALLOW: RunEventType.PERMISSION_GRANTED,
    DecisionEffect.DENY: RunEventType.PERMISSION_DENIED,
    DecisionEffect.REQUIRES_APPROVAL: RunEventType.PERMISSION_REQUESTED,
}
_MESSAGE_KINDS = {StepKind.PLAN, StepKind.MESSAGE, StepKind.OUTPUT, StepKind.HANDOFF}


def _line(text: str | None, limit: int = 120) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else f"{flat[: limit - 1]}…"


def events_for_step(step: Step) -> list[RunEventDraft]:
    """The events one agent step produces (ADR-0013 mapping; pure).

    Text in payloads is the step's own text; callers redact and truncate before
    storing. Observations about a tool call are folded into that call's events.
    """
    kind = step.kind
    call = step.tool_call
    if kind in _MESSAGE_KINDS:
        text = step.output or step.thought or step.observation or ""
        if not text and kind is not StepKind.HANDOFF:
            return []
        return [
            RunEventDraft(
                type=RunEventType.AGENT_MESSAGE,
                summary=_line(text) or kind.value,
                payload={"kind": kind.value, "text": text, "metadata": dict(step.metadata)},
            )
        ]
    if kind is StepKind.OBSERVATION:
        if call is not None or not step.observation:
            return []
        return [
            RunEventDraft(
                type=RunEventType.AGENT_MESSAGE,
                summary=_line(step.observation),
                payload={"kind": kind.value, "text": step.observation},
            )
        ]
    if kind is StepKind.DECISION and call is not None and step.decision is not None:
        decision = step.decision
        return [
            RunEventDraft(
                type=RunEventType.TOOL_REQUESTED,
                summary=f"{call.tool} requested",
                payload={"tool": call.tool, "action": call.action, "arguments": call.arguments},
            ),
            RunEventDraft(
                type=_PERMISSION_BY_EFFECT[decision.effect],
                summary=f"{call.tool}: {decision.effect.value}"
                + (f" ({decision.matched_rule})" if decision.matched_rule else ""),
                payload={
                    "tool": call.tool,
                    "effect": decision.effect.value,
                    "rule": decision.matched_rule,
                    "reason": decision.reason,
                    "authority": "forge_policy",
                },
            ),
        ]
    if kind is StepKind.TOOL_CALL and call is not None:
        events = [
            RunEventDraft(
                type=RunEventType.TOOL_COMPLETED,
                summary=f"{call.tool} ran",
                payload={"tool": call.tool, "ok": step.output is not None, "output": step.output},
            )
        ]
        if call.tool == "git_commit" and step.output is not None:
            events.append(
                RunEventDraft(
                    type=RunEventType.GIT_CHANGED,
                    summary=_line(step.output) or "committed",
                    payload={"output": step.output},
                )
            )
        return events
    return []
