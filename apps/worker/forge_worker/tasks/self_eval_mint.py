"""Self-Eval Gate (F41) minting worker task.

As the org's own PRs merge, this task mints a hidden regression case from each
(:func:`forge_eval.mint.mint_case_from_pr`) and appends it to the workspace's
*private* benchmark suite dir, then **re-freezes** the manifest
(:func:`forge_eval.benchmark.manifest.freeze`) so a fresh ``content_hash`` pins
the grown case set. Unlike a published leaderboard suite, a per-repo private
suite is a living accumulator: :func:`append_minted_cases` unfreezes, writes the
minted case file, and re-freezes in one atomic step.

The pure functions (:func:`append_minted_cases`, :func:`mint_and_store`) carry
the logic and are exercised offline in tests; the ``@celery_app.task`` wrapper is
the thin prod seam. Minted hidden test ids live only on disk in the suite dir and
never enter a model prompt.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from forge_eval.benchmark.manifest import freeze, load_manifest
from forge_eval.golden import GoldenCase, load_golden_set
from forge_eval.mint import PullRequestSource, TestRunner, mint_case_from_pr
from forge_worker.celery_app import celery_app

__all__ = [
    "MINTED_CASE_FILE",
    "append_minted_cases",
    "mint_and_store",
    "self_eval_mint_task",
]

#: Relative path (within a suite version dir) the minted cases accumulate into.
MINTED_CASE_FILE = "cases/self_eval.json"


def _case_to_dict(case: GoldenCase) -> dict[str, Any]:
    return asdict(case)


def _load_case_files(version_dir: Path, case_files: list[str]) -> list[GoldenCase]:
    cases: list[GoldenCase] = []
    seen: set[str] = set()
    for rel in case_files:
        for case in load_golden_set(version_dir / rel):
            if case.id in seen:
                raise ValueError(f"duplicate benchmark case id across files: {case.id!r}")
            seen.add(case.id)
            cases.append(case)
    return cases


def append_minted_cases(
    version_dir: str | Path,
    new_cases: list[GoldenCase],
    *,
    minted_case_file: str = MINTED_CASE_FILE,
) -> int:
    """Append ``new_cases`` to a private suite's minted case file + re-freeze.

    Idempotent on case id: a minted case whose id already exists is updated in
    place, not duplicated. Writes the merged manifest back to
    ``version_dir/manifest.yaml`` with a freshly recomputed ``content_hash``.
    Returns the number of *newly added* cases.
    """
    resolved = Path(version_dir)
    manifest, _existing = load_manifest(resolved)

    minted_path = resolved / minted_case_file
    prior = load_golden_set(minted_path) if minted_path.is_file() else []
    by_id: dict[str, GoldenCase] = {c.id: c for c in prior}
    added = 0
    for case in new_cases:
        if case.id not in by_id:
            added += 1
        by_id[case.id] = case
    minted = sorted(by_id.values(), key=lambda c: c.id)

    minted_path.parent.mkdir(parents=True, exist_ok=True)
    minted_path.write_text(
        json.dumps([_case_to_dict(c) for c in minted], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    case_files = list(manifest.case_files)
    if minted_case_file not in case_files:
        case_files.append(minted_case_file)

    all_cases = _load_case_files(resolved, case_files)
    unfrozen = manifest.model_copy(
        update={"frozen": False, "content_hash": None, "case_files": case_files}
    )
    reforged = freeze(unfrozen, all_cases)

    import yaml  # lazy: mirrors manifest.py's optional PyYAML dependency

    (resolved / "manifest.yaml").write_text(
        yaml.safe_dump(reforged.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    return added


def mint_and_store(
    prs: list[Any],
    repo: str,
    version_dir: str | Path,
    *,
    source: PullRequestSource,
    runner: TestRunner,
    sandbox_image: str | None = None,
    setup_commands: list[str] | None = None,
    max_pass_to_pass: int = 10,
) -> list[str]:
    """Mint a case per merged PR and append the non-empty ones to the suite.

    Returns the ids of the cases that were minted (PRs with no fail-to-pass
    signal are silently skipped — not faked). PR data and test outcomes are
    injected, so this runs fully offline in tests.
    """
    minted: list[GoldenCase] = []
    for pr in prs:
        case = mint_case_from_pr(
            pr,
            repo,
            source=source,
            runner=runner,
            sandbox_image=sandbox_image,
            setup_commands=setup_commands or [],
            max_pass_to_pass=max_pass_to_pass,
        )
        if case is not None:
            minted.append(case)
    if minted:
        append_minted_cases(version_dir, minted)
    return [c.id for c in minted]


@celery_app.task(name="forge.self_eval.mint", queue="self_eval")
def self_eval_mint_task(repo: str, pr_number: int, version_dir: str) -> dict[str, Any]:
    """Prod seam: mint a case for one merged PR into a workspace private suite.

    Builds a GitHub-backed :class:`PullRequestSource` + worktree
    :class:`TestRunner` and delegates to :func:`mint_and_store`. Import of the
    integration SDK is lazy so the pure minting logic (and its tests) never pull
    ``httpx``/``forge_integrations`` into scope.
    """
    import os

    from forge_contracts import PullRequest
    from forge_eval.mint import GitHubPullRequestSource, GitWorktreeTestRunner
    from forge_integrations.github import GitHubClient

    client = GitHubClient(token=os.environ.get("GITHUB_TOKEN"))
    source = GitHubPullRequestSource(client=client)
    runner = GitWorktreeTestRunner(repo_path=version_dir)
    pr = PullRequest(repo=repo, number=pr_number, head_sha=client.pr_head_commit(repo, pr_number))
    minted = mint_and_store([pr], repo, version_dir, source=source, runner=runner)
    return {"repo": repo, "pr": pr_number, "minted": minted}
