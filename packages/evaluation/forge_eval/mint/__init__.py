"""Self-Eval Gate (F41) case minting.

Auto-derives a *private, per-repo* regression benchmark case from one of the
org's own merged PRs: the PR's added/changed tests become the hidden
``fail_to_pass`` set (they fail on ``base_commit`` and pass after the merge), a
sample of untouched green tests becomes ``pass_to_pass``, and the sandbox
image/setup wire the whole thing to a reproducible harness. The hidden test ids
live only in ``GoldenCase.metadata`` (parsed via
:func:`forge_eval.benchmark.swe_case.parse_swe_case_fields`) and never enter a
model's context.

Everything here is deterministic and offline: PR data arrives through the
:class:`PullRequestSource` protocol (a fake in tests, a GitHub adapter in prod)
and test outcomes through the :class:`TestRunner` protocol (the
:class:`GitWorktreeTestRunner` default is the ``SandboxKind.WORKTREE`` analogue —
a local git worktree + ``pytest`` subprocess, no network).
"""

from __future__ import annotations

from forge_eval.mint.pr_miner import (
    ChangedFile,
    GitHubPullRequestSource,
    GitWorktreeTestRunner,
    PullRequestSource,
    TestRunner,
    changed_test_node_ids,
    mint_case_from_pr,
)

__all__ = [
    "ChangedFile",
    "GitHubPullRequestSource",
    "GitWorktreeTestRunner",
    "PullRequestSource",
    "TestRunner",
    "changed_test_node_ids",
    "mint_case_from_pr",
]
