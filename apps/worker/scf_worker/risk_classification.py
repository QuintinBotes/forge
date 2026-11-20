"""Classify an agent run's change and record it (spec A §6, #193).

At close-out, a run that made commits is classified when the repository policy
has ``risk_rules``: the facts of its diff (:func:`signals_from_diff`) give the
derived class, and one append-only ``risk_classification`` row per agent run
records it. The rules come from the default branch's policy, as the diff gates'
do, so a change cannot loosen its own classification. Nothing here alters the run: without
``risk_rules`` nothing is computed, and any failure is logged and swallowed.
"""

from __future__ import annotations

import logging
import subprocess
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from scf_api.services.attestation_service import spec_and_prs
from scf_contracts import AgentObjective, AgentRunResult, ChangeSignals, RiskClass
from scf_db.risk_repository import RiskRepository
from scf_risk import derive, evaluate, requirements_for
from scf_risk.signals import signals_from_changes
from scf_worker.review_rules import default_branch_policy

__all__ = ["classify_change", "signals_from_diff"]

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 60.0


def _numstat(output: str) -> Iterator[tuple[str, int, int]]:
    """``(path, added, removed)`` per ``added<TAB>removed<TAB>path<NUL>`` record."""
    for record in output.split("\0"):
        if record:
            added, removed, path = record.split("\t", 2)
            # A binary file counts "-" lines: it adds and removes no lines of text.
            yield path, int(added) if added != "-" else 0, int(removed) if removed != "-" else 0


def signals_from_diff(repo: Path, base: str, head: str) -> ChangeSignals:
    """The facts of the diff from ``base`` to ``head`` in ``repo``; never raises.

    When the diff cannot be read the signals are marked unavailable, so the
    classifier applies the policy's fallback class.
    """
    try:
        if base.startswith("-") or head.startswith("-"):  # git would read it as an option
            raise ValueError("a ref may not start with '-'")
        output = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "diff",
                "--numstat",
                "-z",
                "--no-renames",
                "--no-ext-diff",
                "--no-textconv",
                base,
                head,
                "--",
            ],
            check=True,
            capture_output=True,
            timeout=_GIT_TIMEOUT,
        ).stdout.decode("utf-8", "replace")
        return signals_from_changes(_numstat(output))
    except Exception as exc:
        return ChangeSignals(
            available=False,
            unavailable_reason=f"diff {base}..{head} unreadable: {type(exc).__name__}",
        )


def classify_change(
    objective: AgentObjective,
    result: AgentRunResult,
    *,
    run_id: uuid.UUID,
    workspace_id: uuid.UUID,
    sessions: Callable[[], sessionmaker[Session]],
    redact: Callable[[str], str],
) -> None:
    """Record the classification of the run's change, if it made commits and rules exist.

    Best effort: a failure is logged and the run completes as it would have.
    """
    try:
        _classify(objective, result, run_id, workspace_id, sessions, redact)
    except Exception:
        logger.exception("could not classify the change of agent run %s (non-fatal)", run_id)


def _classify(
    objective: AgentObjective,
    result: AgentRunResult,
    run_id: uuid.UUID,
    workspace_id: uuid.UUID,
    sessions: Callable[[], sessionmaker[Session]],
    redact: Callable[[str], str],
) -> None:
    artifacts = result.artifacts
    base, head = artifacts.get("base_commit"), artifacts.get("head_commit")
    repo_path = objective.context.get("repo_path")
    if not (base and head and repo_path) or base == head:
        return  # no commits, nothing to classify
    repo = Path(str(repo_path))
    policy = default_branch_policy(repo)
    rules = policy.risk_rules if policy is not None else None
    if rules is None:
        return
    signals = signals_from_diff(repo, str(base), str(head))
    evaluation = evaluate(derive(signals, rules), None)  # no change declares a class yet
    snapshot = signals.model_dump(mode="json") | {"paths": [redact(p) for p in signals.paths]}
    pull_request = artifacts.get("pull_request") or {}
    workflow_run_id = objective.context.get("workflow_run_id")
    with sessions()() as session:
        risk = RiskRepository(session)
        if risk.list_classifications(workspace_id, agent_run_id=run_id, limit=1):
            return  # a retried close-out (#239): the run is classified once
        # The change's identity as its attestation reads it (#193).
        spec_key, spec_version, link_prs = spec_and_prs(session, workspace_id, objective.task_id)
        risk.record_classification(
            workspace_id,
            evaluation,
            commit_sha=str(head),
            signals=snapshot,
            enforcement_mode=rules.enforcement,
            # The pull request this close-out opened; a workflow run opens its own later.
            pr_numbers=[pull_request["number"]] if pull_request.get("number") else link_prs,
            agent_run_id=run_id,
            workflow_run_id=uuid.UUID(str(workflow_run_id)) if workflow_run_id else None,
            spec_key=spec_key or None,
            spec_version=spec_version or None,
            # What each class requires of the gates, so they need not read the policy (#194).
            class_requirements={
                c.value: requirements_for(c, rules).model_dump(mode="json") for c in RiskClass
            },
        )
        session.commit()
