"""``RunRecording`` — an immutable persisted cassette for Time-Travel Runs.

(cassette-persistence) The storage substrate backing deterministic
record-replay of agent runs: one append-only row per recorded run, holding the
redacted :class:`~forge_agent.replay.cassette.RunCassette` snapshot (LLM calls
and tool calls, each indexed by call-order, plus the redacted env) the run was
taped under.

Determinism note (see the slice's grounded seams): the target providers 400 on
``seed``/``temperature`` (``providers/translate.py``), so replay is never by
re-seeding the model — it substitutes the recorded response/result back in by
call-index. ``content_hash`` is a content fingerprint of the persisted
``cassette`` (sha256 of its canonical JSON), independent of the per-call
``request_digest``/``args_digest`` values already inside it; the correctness
net for an individual replayed call is the divergence canary in
``forge_agent.replay.player``, not this column.

Append-only, mirroring ``attestation``: the table opts into the shared
Postgres ``attach_immutability_trigger`` (BEFORE UPDATE/DELETE -> raise); on
SQLite (unit tests) the "insert only, never update" convention at the caller
level is the sole guard, same as every other append-only table here.

``workflow_run_id``/``agent_run_id`` are nullable ``CASCADE`` FKs mirroring
``Attestation`` exactly (see its docstring for the accepted CASCADE-vs-
immutability-trigger tension): a recording may be attached to only one of the
two depending on where the recorder wrapper was installed (single-agent vs.
workflow-level), or to neither yet (recorded before the run is linked).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from forge_db.base import WorkspaceScopedModel, attach_immutability_trigger, json_type


class RunRecording(WorkspaceScopedModel):
    """One immutable recorded cassette (LLM + tool calls) for a run."""

    __tablename__ = "run_recording"
    __table_args__ = (
        Index("ix_run_recording_workspace_created", "workspace_id", "created_at"),
        Index("ix_run_recording_workflow_run_id", "workflow_run_id"),
        Index("ix_run_recording_agent_run_id", "agent_run_id"),
    )

    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_run.id", ondelete="CASCADE"), nullable=True
    )
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workflow_run.id", ondelete="CASCADE"), nullable=True
    )
    #: The redacted ``RunCassette.to_dict()`` snapshot — LLM calls, tool calls,
    #: and the env, keyed by call-index for replay-by-substitution.
    cassette: Mapped[dict[str, Any]] = mapped_column(json_type(), nullable=False)
    #: The model id the run was driven under (the last-known ``ModelResponse
    #: .model`` recorded on the tape), mirroring ``AgentRun.model``. Nullable —
    #: a cassette with zero LLM calls (tool-only run) has no model to report.
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: sha256 hex of the canonical (sorted-key) JSON encoding of ``cassette`` —
    #: a whole-tape content fingerprint for dedup/integrity checks.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


# Postgres: append-only via the shared BEFORE UPDATE/DELETE trigger.
attach_immutability_trigger(RunRecording.__table__)


__all__ = ["RunRecording"]
