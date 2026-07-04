"""Aggregate merge gate + dependency-ordered, halt-on-failure merger (F22).

GitHub offers no cross-repo atomic merge, so F22 ships the strongest practical
guarantee:

* :class:`MultiRepoMergeGate` — an *all-or-nothing* pre-merge gate: no PR merges
  until **every** required, changed repo PR is review-approved + CI-green +
  spec-validated. Otherwise it reports each unmet condition, repo-prefixed.
* :class:`MultiRepoMerger` — re-checks the full gate immediately before the
  first merge, then merges in dependency order, **halting on the first failure**
  and recording exactly which repos merged (the residual partial-merge window is
  escalated to a human, never auto-reverted — a documented V2 limitation).

The gate/merger operate on plain DTOs and a tiny ``RepoMergeClient`` protocol so
they are exercised end-to-end with an in-memory fake GitHub adapter and no DB.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from forge_contracts import (
    MergeGroupOutcome,
    MultiRepoMergeGateResult,
    PRGroup,
    RepoMergeStatus,
)


@runtime_checkable
class RepoMergeClient(Protocol):
    """The slice of a GitHub integration the merger needs (one repo at a time).

    Implementations use *that repo's* installation token (F22 AC 16); a failed
    merge is signalled by returning ``False`` or raising.
    """

    def merge_pr(self, repo: str, pr_number: int) -> bool: ...


class MultiRepoMergeGate:
    """Aggregate per-repo mergeability into a single gate result."""

    @staticmethod
    def evaluate(
        statuses: list[RepoMergeStatus],
        merge_order: list[str] | None = None,
    ) -> MultiRepoMergeGateResult:
        """Aggregate ``statuses`` into a :class:`MultiRepoMergeGateResult`.

        ``can_merge`` is true **iff every required, changed repo** is
        review-approved AND CI-green AND spec-validated. Repos that are not
        ``required_for_merge`` or have no changes are excluded from the gate.
        Each unmet condition is recorded prefixed by its repo id.
        """
        evaluated: list[RepoMergeStatus] = []
        flat: list[str] = []
        for status in statuses:
            reasons: list[str] = []
            if status.required_for_merge and status.has_changes:
                if not status.review_approved:
                    reasons.append(f"{status.repo_id}: review not approved")
                if not status.ci_green:
                    reasons.append(f"{status.repo_id}: CI not green")
                if not status.spec_validated:
                    reasons.append(f"{status.repo_id}: spec not validated")
            # Fold in any externally-supplied reasons, repo-prefixed.
            prefix = f"{status.repo_id}:"
            for reason in status.blocking_reasons:
                reasons.append(reason if reason.startswith(prefix) else f"{prefix} {reason}")
            evaluated.append(status.model_copy(update={"blocking_reasons": reasons}))
            flat.extend(reasons)

        order = merge_order if merge_order is not None else [s.repo_id for s in statuses]
        return MultiRepoMergeGateResult(
            can_merge=not flat,
            repos=evaluated,
            merge_order=order,
            blocking_reasons=flat,
        )


class MultiRepoMerger:
    """Merge a PR group in dependency order, halting on the first failure."""

    def __init__(self, github: RepoMergeClient) -> None:
        self._github = github

    def merge_in_order(
        self,
        group: PRGroup,
        statuses: list[RepoMergeStatus],
    ) -> MergeGroupOutcome:
        """Re-check the gate, then merge ``group`` PRs in ``merge_order``.

        * If the re-checked gate is not green → merge **nothing**, return
          ``status="blocked"`` with reasons (AC 13).
        * On the first failed merge → **halt** (no further merges, no rollback),
          record ``merged_repo_ids`` so far, return ``"partially_merged"`` (or
          ``"failed"`` if the very first merge fails) and route the workflow to
          ``needs_human_input`` (AC 12).
        * On full success → ``"merged"`` and route the workflow to ``merged``.
        """
        gate = MultiRepoMergeGate.evaluate(statuses, group.merge_order)
        if not gate.can_merge:
            return MergeGroupOutcome(
                status="blocked",
                merged_repo_ids=[],
                gate=gate,
                workflow_state="awaiting_review",
            )

        links = {pr.repo_id: pr for pr in group.prs if pr.pr_number is not None}
        ordered = [repo for repo in group.merge_order if repo in links]

        merged: list[str] = []
        for repo in ordered:
            pr = links[repo]
            assert pr.pr_number is not None  # guaranteed by the `links` filter
            try:
                ok = bool(self._github.merge_pr(repo, pr.pr_number))
            except Exception:
                ok = False
            if not ok:
                return MergeGroupOutcome(
                    status="partially_merged" if merged else "failed",
                    merged_repo_ids=merged,
                    failed_repo_id=repo,
                    gate=gate,
                    workflow_state="needs_human_input",
                )
            merged.append(repo)

        return MergeGroupOutcome(
            status="merged",
            merged_repo_ids=merged,
            gate=gate,
            workflow_state="merged",
        )


__all__ = [
    "MultiRepoMergeGate",
    "MultiRepoMerger",
    "RepoMergeClient",
]
