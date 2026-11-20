"""Change outcomes, reverts and interventions from GitHub deliveries (spec 005 FR-009, #196).

Called by the GitHub webhook after its signature check, beside the PR outcome
feedback of spec J. Every write is keyed so a redelivery writes nothing new:

* ``pull_request`` ``closed`` stores the run's outcome (landed or closed) and its
  time, and for a landing the merge commit's full hash, by which the deploy gate
  finds the change's classification (#335). A later outcome replaces an earlier
  close; a landing is never replaced.
  ``reopened`` removes a close it follows: the pull request is open again.
* A landed pull request whose body carries GitHub's revert reference
  (``Reverts owner/repo#N``) marks change ``N`` of the same repository reverted.
* ``pull_request_review`` ``submitted`` with state ``changes_requested`` records a
  ``review_changes_requested`` intervention, keyed by the review id.
* ``pull_request`` ``synchronize`` whose sender is not the account that opened the
  pull request (SCF's own identity on a run's pull request) records a
  ``foreign_commit`` intervention, keyed by the pushed head.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from scf_db.models import AgentRun, ChangeOutcome, RunIntervention
from scf_db.models.factory_metrics import CHANGE_CLOSED, CHANGE_LANDED
from scf_obs.analytics.factory import FOREIGN_COMMIT, REVIEW_CHANGES_REQUESTED

__all__ = ["record_change_event"]

#: The first line GitHub's "Revert" button writes into the new pull request's body.
_REVERT_RE = re.compile(r"(?m)^Reverts ([\w.-]+/[\w.-]+)#(\d{1,9})\b")
#: A pushed head or a merge commit: SHA-1 or SHA-256 hex. Anything else is not keyed or stored.
_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _time(raw: object) -> datetime:
    """A delivery's ISO 8601 time; now when it is absent or malformed."""
    try:
        return _utc(datetime.fromisoformat(raw)) if isinstance(raw, str) else datetime.now(UTC)
    except ValueError:
        return datetime.now(UTC)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _login(value: object) -> str | None:
    login = value.get("login") if isinstance(value, dict) else None
    return login if isinstance(login, str) and login else None


def record_change_event(
    session: Session,
    *,
    run: AgentRun | None,
    event_type: str,
    payload: dict[str, Any],
    repo_full_name: str | None,
    number: int | None,
) -> None:
    """Record what this delivery says about ``run``'s change; commits the session.

    ``run`` is :func:`resolve_run_for_pull_request`'s answer for the delivery. With
    none, only a revert is recorded, against the change it names.
    """
    pull_request = payload.get("pull_request")
    if not isinstance(pull_request, dict):
        return
    action = payload.get("action")
    if event_type == "pull_request" and action == "closed":
        merged = pull_request.get("merged") is True
        at = _time(pull_request.get("merged_at" if merged else "closed_at"))
        if merged:
            _mark_reverted(session, pull_request.get("body"), repo_full_name, number, at)
        if run is not None and repo_full_name and number is not None:
            outcome = CHANGE_LANDED if merged else CHANGE_CLOSED
            # An unmerged pull request reports a test merge's hash: never the change's.
            merge = pull_request.get("merge_commit_sha") if merged else None
            merge = merge if isinstance(merge, str) and _SHA_RE.fullmatch(merge) else None
            _record_outcome(session, run, repo_full_name, number, outcome, at, merge)
    elif run is None:
        return
    elif event_type == "pull_request" and action == "reopened":
        # Open again until it closes again: the close it undid no longer settles the run.
        existing = session.scalar(select(ChangeOutcome).where(ChangeOutcome.agent_run_id == run.id))
        reopened_at = _time(pull_request.get("updated_at"))
        if (
            existing
            and existing.outcome == CHANGE_CLOSED
            and reopened_at > _utc(existing.outcome_at)
        ):
            session.delete(existing)
    elif event_type == "pull_request" and action == "synchronize":
        sender, author = _login(payload.get("sender")), _login(pull_request.get("user"))
        after = payload.get("after")
        is_sha = isinstance(after, str) and _SHA_RE.fullmatch(after) is not None
        if sender and author and is_sha and sender.casefold() != author.casefold():
            at = _time(pull_request.get("updated_at"))
            _record_intervention(session, run, FOREIGN_COMMIT, f"commit:{after}", at)
    elif event_type == "pull_request_review" and action == "submitted":
        review = payload.get("review")
        review = review if isinstance(review, dict) else {}
        state = str(review.get("state") or "").lower()
        review_id = review.get("id")
        if state == "changes_requested" and type(review_id) is int:
            at = _time(review.get("submitted_at"))
            _record_intervention(session, run, REVIEW_CHANGES_REQUESTED, f"review:{review_id}", at)
    try:
        session.commit()
    except IntegrityError:  # a concurrent redelivery wrote the same row first
        session.rollback()


def _record_outcome(
    session: Session,
    run: AgentRun,
    repo: str,
    number: int,
    outcome: str,
    at: datetime,
    merge_commit_sha: str | None,
) -> None:
    existing = session.scalar(select(ChangeOutcome).where(ChangeOutcome.agent_run_id == run.id))
    if existing is None:
        session.add(
            ChangeOutcome(
                workspace_id=run.workspace_id,
                agent_run_id=run.id,
                repo_full_name=repo,
                pr_number=number,
                outcome=outcome,
                outcome_at=at,
                merge_commit_sha=merge_commit_sha,
            )
        )
    elif existing.outcome == CHANGE_CLOSED and at > _utc(existing.outcome_at):
        existing.outcome, existing.outcome_at = outcome, at
        existing.merge_commit_sha = merge_commit_sha


def _record_intervention(
    session: Session, run: AgentRun, kind: str, source_key: str, at: datetime
) -> None:
    seen = session.scalar(
        select(RunIntervention.id).where(
            RunIntervention.agent_run_id == run.id,
            RunIntervention.kind == kind,
            RunIntervention.source_key == source_key,
        )
    )
    if seen is None:
        session.add(
            RunIntervention(
                workspace_id=run.workspace_id,
                agent_run_id=run.id,
                kind=kind,
                source_key=source_key,
                occurred_at=at,
            )
        )


def _mark_reverted(
    session: Session, body: object, repo: str | None, number: int | None, at: datetime
) -> None:
    match = _REVERT_RE.search(body) if isinstance(body, str) else None
    if match is None or not repo or match.group(1).casefold() != repo.casefold():
        return
    original = int(match.group(2))
    if original == number:
        return
    for row in session.scalars(
        select(ChangeOutcome).where(
            ChangeOutcome.repo_full_name == repo,
            ChangeOutcome.pr_number == original,
            ChangeOutcome.outcome == CHANGE_LANDED,
            ChangeOutcome.reverted_at.is_(None),
        )
    ):
        row.reverted_at, row.reverted_by_pr_number = at, number
