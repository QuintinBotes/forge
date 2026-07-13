"""F41 Self-Eval Gate minting — offline (fake GitHub + real fixture git repo).

The GitHub side is a fake :class:`FakePullRequestSource` (no network); the test
side is the *real* :class:`GitWorktreeTestRunner` driving ``pytest`` in a git
worktree of a tiny fixture repo. The fixture PR both adds ``mul`` to ``calc.py``
and adds ``test_mul.py`` — so the new test fails on ``base_commit`` (``mul`` does
not exist) and passes on the merge head: a genuine fail -> pass regression.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from forge_eval.benchmark import parse_swe_case_fields
from forge_eval.mint import ChangedFile, GitWorktreeTestRunner, changed_test_node_ids
from forge_eval.mint.pr_miner import mint_case_from_pr

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git required for worktrees")


@dataclass
class FakePR:
    """Duck-typed on the frozen forge_contracts.PullRequest surface."""

    number: int
    head_sha: str
    title: str = "Add multiply to calc"
    repo: str = "acme/widgets"


class FakePullRequestSource:
    """In-memory PR-data source — the fake GitHub the miner reads through."""

    def __init__(self, *, base_commit: str, changed_files: list[ChangedFile]) -> None:
        self._base = base_commit
        self._files = changed_files

    def pr_changed_files(self, repo: str, number: int) -> list[ChangedFile]:
        return list(self._files)

    def pr_base_commit(self, repo: str, number: int) -> str:
        return self._base


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def _write(repo: Path, rel: str, body: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


@pytest.fixture
def fixture_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A 2-commit git repo: base (no ``mul``) -> merge (adds ``mul`` + its test).

    Returns ``(repo_path, base_sha, head_sha)``.
    """
    repo = tmp_path / "widgets"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Tester")

    # --- base commit: calc.add + a pre-existing green test (pass_to_pass) ---
    _write(repo, "calc.py", "def add(a, b):\n    return a + b\n")
    _write(
        repo,
        "test_add.py",
        "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")

    # --- merge head: PR adds calc.mul AND test_mul.py (fail_to_pass) ---
    _write(
        repo,
        "calc.py",
        "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n",
    )
    _write(
        repo,
        "test_mul.py",
        "from calc import mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add mul")
    head_sha = _git(repo, "rev-parse", "HEAD")

    return repo, base_sha, head_sha


_MUL_PATCH = (
    "@@ -0,0 +1,4 @@\n+from calc import mul\n+\n+\n+def test_mul():\n+    assert mul(2, 3) == 6\n"
)


def test_changed_test_node_ids_parses_added_defs() -> None:
    files = [
        ChangedFile(path="test_mul.py", status="added", patch=_MUL_PATCH),
        ChangedFile(path="calc.py", status="modified", patch="@@ -1 +1,4 @@\n+def mul(a, b):\n"),
    ]
    # Only the test file contributes; the source-file def is ignored.
    assert changed_test_node_ids(files) == ["test_mul.py::test_mul"]


def test_changed_test_node_ids_scopes_class_methods() -> None:
    patch = (
        "@@ -0,0 +1,3 @@\n class TestThing:\n+    def test_method(self):\n+        assert True\n"
    )
    files = [ChangedFile(path="tests/test_thing.py", status="modified", patch=patch)]
    assert changed_test_node_ids(files) == ["tests/test_thing.py::TestThing::test_method"]


def test_mint_case_from_pr_derives_fail_to_pass(fixture_repo: tuple[Path, str, str]) -> None:
    repo, base_sha, head_sha = fixture_repo
    source = FakePullRequestSource(
        base_commit=base_sha,
        changed_files=[
            ChangedFile(path="test_mul.py", status="added", patch=_MUL_PATCH),
            ChangedFile(
                path="calc.py",
                status="modified",
                patch="@@ -1 +1,4 @@\n+def mul(a, b):\n+    return a * b\n",
            ),
        ],
    )
    runner = GitWorktreeTestRunner(repo_path=repo)
    pr = FakePR(number=7, head_sha=head_sha)

    case = mint_case_from_pr(
        pr,
        "acme/widgets",
        source=source,
        runner=runner,
        sandbox_image="python:3.14-slim",
        setup_commands=["pip install -e ."],
    )

    assert case is not None
    assert case.id == "self-eval-acme-widgets-pr-7"
    assert case.kind == "agent_task"
    assert case.expected_ids == ["test_mul.py::test_mul"]

    fields = parse_swe_case_fields(case)
    assert fields.fail_to_pass == ["test_mul.py::test_mul"]
    # The pre-existing green test is sampled as a regression guard.
    assert "test_add.py::test_add" in fields.pass_to_pass
    # The new test itself is never a pass_to_pass entry.
    assert "test_mul.py::test_mul" not in fields.pass_to_pass
    assert fields.base_commit == base_sha
    assert fields.sandbox_image == "python:3.14-slim"
    assert fields.setup_commands == ["pip install -e ."]
    # Never declares a merge terminal state (AC24 human-approval gate).
    assert case.metadata["expected_terminal_state"] != "merged"


def test_mint_returns_none_when_no_regression_signal(fixture_repo: tuple[Path, str, str]) -> None:
    repo, base_sha, head_sha = fixture_repo
    # A PR that added a test which *already passes* on base yields no fail->pass.
    already_green = (
        "@@ -0,0 +1,4 @@\n"
        "+from calc import add\n"
        "+\n"
        "+def test_add_again():\n"
        "+    assert add(2, 2) == 4\n"
    )
    source = FakePullRequestSource(
        base_commit=base_sha,
        changed_files=[ChangedFile(path="test_extra.py", status="added", patch=already_green)],
    )
    # test_extra.py does not exist on either ref of the fixture, so the runner
    # reports it failing on base AND head -> not a fail->pass -> no case.
    runner = GitWorktreeTestRunner(repo_path=repo)
    pr = FakePR(number=8, head_sha=head_sha)
    assert mint_case_from_pr(pr, "acme/widgets", source=source, runner=runner) is None


def test_mint_returns_none_when_no_tests_changed(fixture_repo: tuple[Path, str, str]) -> None:
    repo, base_sha, head_sha = fixture_repo
    source = FakePullRequestSource(
        base_commit=base_sha,
        changed_files=[ChangedFile(path="README.md", status="modified", patch="@@ +1 @@\n+docs\n")],
    )
    runner = GitWorktreeTestRunner(repo_path=repo)
    pr = FakePR(number=9, head_sha=head_sha)
    assert mint_case_from_pr(pr, "acme/widgets", source=source, runner=runner) is None
