"""Mint a Self-Eval Gate benchmark case from a merged PR (F41).

:func:`mint_case_from_pr` turns a merged :class:`~forge_contracts.PullRequest`
into a hidden fail-to-pass / pass-to-pass regression case:

1. Parse the PR's changed-file *patches* for added/changed test node ids
   (:func:`changed_test_node_ids`) — the ``fail_to_pass`` candidates.
2. Run those candidates at ``base_commit`` (before) and at the merge head
   (after); keep the ones that go **fail -> pass** — a real regression the PR
   fixed.
3. Sample a deterministic slice of *pre-existing* tests (untouched by the PR)
   that are green at both refs — the ``pass_to_pass`` regression guard.
4. Emit an ``agent_task`` :class:`~forge_eval.golden.GoldenCase` whose
   ``metadata`` carries the sandbox wiring
   (:class:`~forge_eval.benchmark.swe_case.SweCaseFields`).

PR data comes through :class:`PullRequestSource` and test outcomes through
:class:`TestRunner`, both injectable so the whole flow runs offline against a
fake GitHub + a local fixture repo. No live GitHub, no real provider.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from forge_eval.benchmark.swe_case import parse_swe_case_fields
from forge_eval.golden import GoldenCase

__all__ = [
    "ChangedFile",
    "GitHubPullRequestSource",
    "GitWorktreeTestRunner",
    "PullRequestSource",
    "TestRunner",
    "changed_test_node_ids",
    "mint_case_from_pr",
]

#: A minted case must terminate no later than opening a PR — the harness never
#: merges on the model's behalf (AC24 human-approval gate). ``validate_freezable``
#: rejects ``expected_terminal_state == "merged"``.
_TERMINAL_STATE = "pr_opened"

#: Patterns that mark a path as a test module (pytest's default discovery).
_TEST_FILE_RE = re.compile(r"(^|/)(test_[^/]+|[^/]+_test)\.py$")
#: An added test def inside a unified-diff hunk (``+`` marker already stripped).
_DEF_RE = re.compile(r"^(?P<indent>\s*)(?:async\s+)?def\s+(?P<name>test\w+)\s*\(")
#: A class header (context or added) inside a hunk, used to scope test methods.
_CLASS_RE = re.compile(r"^\s*class\s+(?P<name>Test\w+)\b")


def _is_test_path(path: str) -> bool:
    return bool(_TEST_FILE_RE.search(path))


@dataclass(frozen=True)
class ChangedFile:
    """One file touched by a PR, with its unified-diff ``patch`` (GitHub shape)."""

    path: str
    status: str = "modified"  # added | modified | removed | renamed
    patch: str = ""


@runtime_checkable
class PullRequestSource(Protocol):
    """PR data seam — a fake in tests, a GitHub adapter in prod.

    Kept minimal on purpose: the miner only needs the changed-file patches (to
    find the added/changed tests) and the base commit (to run them "before").
    """

    def pr_changed_files(self, repo: str, number: int) -> list[ChangedFile]: ...

    def pr_base_commit(self, repo: str, number: int) -> str: ...


@runtime_checkable
class TestRunner(Protocol):
    """Runs/collects tests at a git ref. The offline sandbox seam.

    ``run_tests`` returns ``node_id -> passed`` for exactly the requested ids
    (a missing/erroring test is reported ``False``). ``collect_tests`` lists the
    node ids discoverable at ``ref`` (used to pick ``pass_to_pass`` candidates).
    """

    def run_tests(self, *, ref: str, node_ids: Sequence[str]) -> dict[str, bool]: ...

    def collect_tests(self, *, ref: str) -> list[str]: ...


def changed_test_node_ids(files: Sequence[ChangedFile]) -> list[str]:
    """Extract pytest node ids for tests *added or changed* by a PR.

    Reads the ``+`` (added) lines of each test file's unified-diff ``patch`` and
    emits ``path::test_name`` (or ``path::TestClass::test_name`` for a method
    whose enclosing ``class Test*`` is visible in the same hunk). Deterministic,
    de-duplicated, and order-stable across files.
    """
    seen: set[str] = set()
    node_ids: list[str] = []
    for changed in files:
        if changed.status == "removed" or not _is_test_path(changed.path):
            continue
        current_class: str | None = None
        for raw in changed.patch.splitlines():
            if raw.startswith(("+++", "---")) or raw.startswith("@@"):
                current_class = None if raw.startswith("@@") else current_class
                continue
            marker, body = (raw[:1], raw[1:]) if raw[:1] in "+- " else ("", raw)
            if marker == "-":  # a removed line never contributes an added test
                continue
            class_match = _CLASS_RE.match(body)
            if class_match:
                current_class = class_match.group("name")
                continue
            if marker != "+":  # only added lines yield new/changed tests
                continue
            def_match = _DEF_RE.match(body)
            if not def_match:
                continue
            name = def_match.group("name")
            if def_match.group("indent") and current_class:
                node_id = f"{changed.path}::{current_class}::{name}"
            else:
                node_id = f"{changed.path}::{name}"
            if node_id not in seen:
                seen.add(node_id)
                node_ids.append(node_id)
    return node_ids


def _sample_pass_to_pass(
    runner: TestRunner,
    *,
    base_commit: str,
    head_ref: str,
    excluded_files: set[str],
    excluded_ids: set[str],
    limit: int,
) -> list[str]:
    """Deterministic sample of pre-existing tests that stay green base -> head."""
    if limit <= 0:
        return []
    candidates = sorted(
        node_id
        for node_id in runner.collect_tests(ref=base_commit)
        if node_id not in excluded_ids and node_id.split("::", 1)[0] not in excluded_files
    )[: limit * 3]  # over-sample; some may be flaky/absent at head
    if not candidates:
        return []
    base_ok = runner.run_tests(ref=base_commit, node_ids=candidates)
    head_ok = runner.run_tests(ref=head_ref, node_ids=candidates)
    green = [nid for nid in candidates if base_ok.get(nid) and head_ok.get(nid)]
    return green[:limit]


def _default_case_id(repo: str, number: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", repo.lower()).strip("-")
    return f"self-eval-{slug}-pr-{number}"


def mint_case_from_pr(
    pr: Any,
    repo: str,
    *,
    source: PullRequestSource,
    runner: TestRunner,
    sandbox_image: str | None = None,
    setup_commands: Sequence[str] = (),
    max_pass_to_pass: int = 10,
    prompt: str | None = None,
    case_id: str | None = None,
) -> GoldenCase | None:
    """Derive a Self-Eval Gate :class:`GoldenCase` from a merged ``pr``.

    Returns ``None`` when the PR yields no ``fail_to_pass`` signal (no
    added/changed test went fail -> pass) — such a PR can't seed a regression
    case and is skipped rather than faked.

    ``pr`` is duck-typed on the frozen :class:`~forge_contracts.PullRequest`
    surface (``.number``, ``.title``, ``.head_sha``). ``sandbox_image`` /
    ``setup_commands`` describe the reproducible env the case replays in.
    """
    number = int(pr.number)
    head_ref = str(pr.head_sha)
    base_commit = source.pr_base_commit(repo, number)
    changed = source.pr_changed_files(repo, number)

    candidates = changed_test_node_ids(changed)
    if not candidates:
        return None

    base_outcomes = runner.run_tests(ref=base_commit, node_ids=candidates)
    head_outcomes = runner.run_tests(ref=head_ref, node_ids=candidates)
    fail_to_pass = sorted(
        nid
        for nid in candidates
        if not base_outcomes.get(nid, False) and head_outcomes.get(nid, False)
    )
    if not fail_to_pass:
        return None

    changed_test_files = {c.path for c in changed if _is_test_path(c.path)}
    pass_to_pass = _sample_pass_to_pass(
        runner,
        base_commit=base_commit,
        head_ref=head_ref,
        excluded_files=changed_test_files,
        excluded_ids=set(candidates),
        limit=max_pass_to_pass,
    )

    metadata: dict[str, Any] = {
        "fail_to_pass": fail_to_pass,
        "pass_to_pass": pass_to_pass,
        "sandbox_image": sandbox_image,
        "setup_commands": list(setup_commands),
        "base_commit": base_commit,
        "expected_terminal_state": _TERMINAL_STATE,
        "source_repo": repo,
        "source_pr": number,
        "source_commit": head_ref,
    }
    case = GoldenCase(
        id=case_id or _default_case_id(repo, number),
        query=prompt or (getattr(pr, "title", None) or f"Resolve {repo}#{number}"),
        expected_ids=fail_to_pass,
        kind="agent_task",
        tags=["self-eval"],
        metadata=metadata,
    )
    # Fail loudly if the sandbox wiring is malformed rather than shipping a case
    # the harness can't replay.
    parse_swe_case_fields(case)
    return case


# --------------------------------------------------------------------------- #
# Concrete adapters                                                             #
# --------------------------------------------------------------------------- #


@dataclass
class GitHubPullRequestSource:
    """:class:`PullRequestSource` backed by ``forge_integrations`` GitHub client.

    ``client`` is duck-typed on
    :meth:`forge_integrations.github.GitHubClient.list_pr_files` /
    :meth:`~forge_integrations.github.GitHubClient.pr_base_commit`, so this
    module never imports ``forge_integrations`` (keeping ``forge_eval`` free of
    that dependency); tests inject a fake implementing the same two methods.
    """

    client: Any

    def pr_changed_files(self, repo: str, number: int) -> list[ChangedFile]:
        files: list[ChangedFile] = []
        for raw in self.client.list_pr_files(repo, number):
            files.append(
                ChangedFile(
                    path=str(raw.get("filename") or raw.get("path") or ""),
                    status=str(raw.get("status") or "modified"),
                    patch=str(raw.get("patch") or ""),
                )
            )
        return [f for f in files if f.path]

    def pr_base_commit(self, repo: str, number: int) -> str:
        return str(self.client.pr_base_commit(repo, number))


@dataclass
class GitWorktreeTestRunner:
    """Offline ``TestRunner`` — a git worktree + ``pytest`` subprocess per ref.

    The ``SandboxKind.WORKTREE`` analogue used by tests and the V1 minter: for a
    ref it detaches a throwaway worktree of ``repo_path`` at that commit and runs
    ``pytest`` there. Fully local, no network. A node id absent/erroring at a ref
    is reported ``False`` (via pytest's non-zero exit), which is exactly the
    "test didn't exist yet on base_commit" signal the miner keys fail-to-pass on.
    """

    repo_path: str | Path
    pytest_args: Sequence[str] = field(default_factory=lambda: ("-p", "no:cacheprovider"))
    timeout_s: int = 300

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.repo_path), *args],
            capture_output=True,
            text=True,
            timeout=self.timeout_s,
            check=False,
        )

    def _pytest(self, worktree: str, extra: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "pytest", *self.pytest_args, *extra],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=self.timeout_s,
            check=False,
        )

    def _with_worktree(self, ref: str) -> tuple[str, Callable[[], None]]:
        tmp = tempfile.mkdtemp(prefix="forge-selfeval-")
        added = self._git("worktree", "add", "--detach", "--force", tmp, ref)
        if added.returncode != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            raise RuntimeError(f"git worktree add {ref!r} failed: {added.stderr.strip()}")

        def cleanup() -> None:
            self._git("worktree", "remove", "--force", tmp)
            shutil.rmtree(tmp, ignore_errors=True)

        return tmp, cleanup

    def run_tests(self, *, ref: str, node_ids: Sequence[str]) -> dict[str, bool]:
        if not node_ids:
            return {}
        worktree, cleanup = self._with_worktree(ref)
        try:
            # Run each node id in isolation so a collection error on one absent
            # test never taints the verdict for the others.
            return {nid: self._pytest(worktree, [nid, "-q"]).returncode == 0 for nid in node_ids}
        finally:
            cleanup()

    def collect_tests(self, *, ref: str) -> list[str]:
        worktree, cleanup = self._with_worktree(ref)
        try:
            proc = self._pytest(worktree, ["--collect-only", "-q"])
            node_ids: list[str] = []
            for line in proc.stdout.splitlines():
                candidate = line.strip()
                if "::" in candidate and not candidate.startswith(("<", "warning")):
                    node_ids.append(candidate)
            return node_ids
        finally:
            cleanup()
