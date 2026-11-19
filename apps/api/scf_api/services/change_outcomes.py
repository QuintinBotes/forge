"""Change outcomes, reverts and interventions from GitHub deliveries (spec 005 FR-009, #196).

Called by the GitHub webhook after its signature check, beside the PR outcome
feedback of spec J. Every write is keyed so a redelivery writes nothing new:

* ``pull_request`` ``closed`` stores the run's outcome (landed or closed) and its
  time. A later outcome replaces an earlier close (a reopened pull request that
  lands ends landed); a landing is never replaced.
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

from scf_api.services.context_service import resolve_run_for_pull_request
from scf_db.models import AgentRun, ChangeOutcome, RunIntervention
from scf_db.models.factory_metrics import CHANGE_CLOSED, CHANGE_LANDED
from scf_obs.analytics.factory import FOREIGN_COMMIT, REVIEW_CHANGES_REQUESTED

__all__ = ["record_change_event"]

#: The first line GitHub's "Revert" button writes into the new pull request's body.
_REVERT_RE = re.compile(r"(?m)^Reverts ([\w.-]+/[\w.-]+)#(\d{1,9})\b")
#: A pushed head: SHA-1 or SHA-256 hex. Anything else is not keyed or stored.
_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _time(raw: object) -> datetime:
    """A delivery's ISO 8601 time; now when it is absent or malformed."""
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            pass
        else:
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return datetime.now(UTC)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _login(value: object) -> str | None:
    login = value.get("login") if isinstance(value, dict) else None
    return login if isinstance(login, str) and login else None


def record_change_event(
    session: Session,
    *,
    event_type: str,
    payload: dict[str, Any],
    repo_full_name: str | None,
    number: int | None,
    head_ref: str | None,
) -> None:
    """Record what this delivery says about a run's change; commits the session."""
    pull_request = payload.get("pull_request")
    if not isinstance(pull_request, dict):
        return
    action = payload.get("action")

    def run() -> AgentRun | None:
        return resolve_run_for_pull_request(
            session, repo_full_name=repo_full_name, number=number, head_ref=head_ref
        )

    if event_type == "pull_request" and action == "closed":
        merged = pull_request.get("merged") is True
        at = _time(pull_request.get("merged_at" if merged else "closed_at"))
        if merged:
            _mark_reverted(session, pull_request.get("body"), repo_full_name, number, at)
        found = run()
        if found is not None and repo_full_name and number is not None:
            outcome = CHANGE_LANDED if merged else CHANGE_CLOSED
            _record_outcome(session, found, repo_full_name, number, outcome, at)
    elif event_type == "pull_request" and action == "synchronize":
        sender, author = _login(payload.get("sender")), _login(pull_request.get("user"))
        after = payload.get("after")
        is_sha = isinstance(after, str) and _SHA_RE.fullmatch(after) is not None
        if sender and author and is_sha and sender.casefold() != author.casefold():
            found = run()
            if found is not None:
                _record_intervention(
                    session,
                    found,
                    FOREIGN_COMMIT,
                    f"commit:{after}",
                    _time(pull_request.get("updated_at")),
                )
    elif event_type == "pull_request_review" and action == "submitted":
        review = payload.get("review")
        review = review if isinstance(review, dict) else {}
        state = str(review.get("state") or "").lower()
        review_id = review.get("id")
        if state == "changes_requested" and type(review_id) is int:
            found = run()
            if found is not None:
                _record_intervention(
                    session,
                    found,
                    REVIEW_CHANGES_REQUESTED,
                    f"review:{review_id}",
                    _time(review.get("submitted_at")),
                )
    try:
        session.commit()
    except IntegrityError:  # a concurrent redelivery wrote the same row first
        session.rollback()


def _record_outcome(
    session: Session, run: AgentRun, repo: str, number: int, outcome: str, at: datetime
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
            )
        )
    elif existing.outcome == CHANGE_CLOSED and at > _utc(existing.outcome_at):
        existing.outcome, existing.outcome_at = outcome, at


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
