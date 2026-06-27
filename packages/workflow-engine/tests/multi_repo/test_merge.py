"""F22 — aggregate merge gate + ordered, halt-on-failure merger.

Covers AC 10 (aggregate gate), AC 11 (ordered merge), AC 12 (partial-merge halt),
AC 13 (gate re-check before first merge), AC 16 (per-repo token recorded).
"""

from __future__ import annotations

import pytest

from forge_contracts import CrossPRLink, PRGroup, RepoMergeStatus
from forge_workflow.multi_repo import MultiRepoMergeGate, MultiRepoMerger


class FakeMultiGitHub:
    """In-memory GitHub keyed by repo, recording merge order + repo per call."""

    def __init__(self, *, fail_merge_for: set[str] | None = None) -> None:
        self.fail_merge_for = fail_merge_for or set()
        self.merge_calls: list[tuple[str, int]] = []

    def merge_pr(self, repo: str, pr_number: int) -> bool:
        self.merge_calls.append((repo, pr_number))
        return repo not in self.fail_merge_for


def _ready(repo_id: str, *, order: int) -> RepoMergeStatus:
    return RepoMergeStatus(
        repo_id=repo_id,
        has_changes=True,
        required_for_merge=True,
        review_approved=True,
        ci_green=True,
        spec_validated=True,
        merge_order=order,
    )


def _group(*repo_to_pr: tuple[str, int]) -> PRGroup:
    order = [r for r, _ in repo_to_pr]
    prs = [
        CrossPRLink(repo_id=r, pr_number=n, merge_order=i) for i, (r, n) in enumerate(repo_to_pr)
    ]
    return PRGroup(merge_order=order, prs=prs, status="ready")


# --------------------------------------------------------------------------- #
# gate                                                                         #
# --------------------------------------------------------------------------- #


def test_gate_green_when_all_ready() -> None:
    result = MultiRepoMergeGate.evaluate([_ready("api", order=0), _ready("web", order=1)])
    assert result.can_merge is True
    assert result.blocking_reasons == []


def test_gate_blocks_until_all_repos_ready() -> None:
    """AC 10: one repo unapproved => can_merge False with repo-prefixed reasons."""
    web = _ready("web", order=1).model_copy(update={"review_approved": False})
    result = MultiRepoMergeGate.evaluate([_ready("api", order=0), web])
    assert result.can_merge is False
    assert any(r.startswith("web:") for r in result.blocking_reasons)
    assert not any(r.startswith("api:") for r in result.blocking_reasons)


def test_gate_excludes_no_change_and_not_required() -> None:
    no_change = RepoMergeStatus(repo_id="proto", has_changes=False)
    not_required = RepoMergeStatus(repo_id="docs", has_changes=True, required_for_merge=False)
    result = MultiRepoMergeGate.evaluate([_ready("api", order=0), no_change, not_required])
    assert result.can_merge is True


# --------------------------------------------------------------------------- #
# merger                                                                       #
# --------------------------------------------------------------------------- #


def test_merge_order_respected() -> None:
    """AC 11: merge_pr is called for api before web."""
    github = FakeMultiGitHub()
    merger = MultiRepoMerger(github)
    outcome = merger.merge_in_order(
        _group(("api", 123), ("web", 456)),
        [_ready("api", order=0), _ready("web", order=1)],
    )
    assert outcome.status == "merged"
    assert outcome.merged_repo_ids == ["api", "web"]
    assert outcome.workflow_state == "merged"
    assert [repo for repo, _ in github.merge_calls] == ["api", "web"]


def test_partial_merge_halts() -> None:
    """AC 12: web merge fails after api merged => partially_merged, no rollback."""
    github = FakeMultiGitHub(fail_merge_for={"web"})
    merger = MultiRepoMerger(github)
    outcome = merger.merge_in_order(
        _group(("api", 123), ("web", 456)),
        [_ready("api", order=0), _ready("web", order=1)],
    )
    assert outcome.status == "partially_merged"
    assert outcome.merged_repo_ids == ["api"]
    assert outcome.failed_repo_id == "web"
    assert outcome.workflow_state == "needs_human_input"
    # No rollback: api stays merged, web was attempted, nothing after web.
    assert [repo for repo, _ in github.merge_calls] == ["api", "web"]


def test_first_merge_failure_is_failed_not_partial() -> None:
    github = FakeMultiGitHub(fail_merge_for={"api"})
    merger = MultiRepoMerger(github)
    outcome = merger.merge_in_order(
        _group(("api", 123), ("web", 456)),
        [_ready("api", order=0), _ready("web", order=1)],
    )
    assert outcome.status == "failed"
    assert outcome.merged_repo_ids == []
    assert outcome.failed_repo_id == "api"
    # Halted before web.
    assert [repo for repo, _ in github.merge_calls] == ["api"]


def test_gate_recheck_before_first_merge_blocks() -> None:
    """AC 13: CI flipped red between approval and merge => merge nothing."""
    github = FakeMultiGitHub()
    merger = MultiRepoMerger(github)
    web_red = _ready("web", order=1).model_copy(update={"ci_green": False})
    outcome = merger.merge_in_order(
        _group(("api", 123), ("web", 456)),
        [_ready("api", order=0), web_red],
    )
    assert outcome.status == "blocked"
    assert outcome.merged_repo_ids == []
    assert github.merge_calls == []
    assert outcome.gate is not None and outcome.gate.can_merge is False


def test_no_diff_repo_excluded_from_merge() -> None:
    """A repo with no PR (no_diff) is not merged even if listed in merge_order."""
    github = FakeMultiGitHub()
    merger = MultiRepoMerger(github)
    group = PRGroup(
        merge_order=["api", "proto", "web"],
        prs=[
            CrossPRLink(repo_id="api", pr_number=1, merge_order=0),
            CrossPRLink(repo_id="proto", pr_number=None, merge_order=1),  # no diff
            CrossPRLink(repo_id="web", pr_number=2, merge_order=2),
        ],
    )
    outcome = merger.merge_in_order(
        group,
        [
            _ready("api", order=0),
            RepoMergeStatus(repo_id="proto", has_changes=False),
            _ready("web", order=2),
        ],
    )
    assert outcome.status == "merged"
    assert [repo for repo, _ in github.merge_calls] == ["api", "web"]


def test_per_repo_token_recorded_via_repo_arg() -> None:
    """AC 16 (at this layer): each merge call carries its own repo id."""
    github = FakeMultiGitHub()
    MultiRepoMerger(github).merge_in_order(
        _group(("api", 1), ("web", 2)),
        [_ready("api", order=0), _ready("web", order=1)],
    )
    assert github.merge_calls == [("api", 1), ("web", 2)]


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
