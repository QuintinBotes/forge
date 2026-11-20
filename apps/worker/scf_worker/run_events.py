"""Append a run's events as it happens (ADR-0013, spec L seam 3).

Each event is redacted, truncated, numbered and committed on its own, so a client
following ``GET /agent/runs/{id}/events`` sees it immediately. Writing an event never
fails the run: an error is logged and the run carries on. :class:`RunEventSink` lives in
``scf_api.services.run_record``, so a run process appending through the API (#256)
stores events with the same code.
"""

from __future__ import annotations

from scf_api.services.run_record import MAX_TEXT, RunEventSink
from scf_contracts import AgentRunResult, RunEventDraft, RunEventType, RunStatus

__all__ = ["MAX_TEXT", "RunEventSink", "terminal_drafts"]


def terminal_drafts(result: AgentRunResult) -> list[RunEventDraft]:
    """``artifact.created`` (if a PR opened), ``cost.updated``, then the terminal event."""
    drafts: list[RunEventDraft] = []
    pull = result.artifacts.get("pull_request")
    if isinstance(pull, dict) and pull.get("status") == "opened":
        drafts.append(
            RunEventDraft(
                type=RunEventType.ARTIFACT_CREATED,
                summary=f"Opened pull request #{pull.get('number')}",
                payload={
                    "kind": "pull_request",
                    "url": pull.get("url"),
                    "number": pull.get("number"),
                },
            )
        )
    usage = result.artifacts.get("model_usage")
    if isinstance(usage, dict):
        drafts.append(
            RunEventDraft(
                type=RunEventType.COST_UPDATED,
                summary=f"{usage.get('input_tokens', 0)} in, {usage.get('output_tokens', 0)} out",
                payload=dict(usage),
            )
        )
    if result.status is RunStatus.SUCCEEDED:
        drafts.append(
            RunEventDraft(
                type=RunEventType.THREAD_COMPLETED,
                summary=result.summary or "Run completed",
                payload={"status": result.status.value},
            )
        )
    else:
        drafts.append(
            RunEventDraft(
                type=RunEventType.THREAD_FAILED,
                summary=result.error or f"Run {result.status.value}",
                payload={"status": result.status.value, "error": result.error},
            )
        )
    return drafts
