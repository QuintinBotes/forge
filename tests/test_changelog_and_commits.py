"""Commitizen behaviour: SemVer bump rules, commit-lint, changelog (AC3, AC4).

Exercises the real ``cz`` CLI against a throwaway temp git repo (no network, no
mutation of the real tree), mirroring how a maintainer runs ``make bump`` /
``make changelog``.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("commitizen") is None,
    reason="commitizen not installed",
)

_PYPROJECT = """\
[project]
name = "tmp-cz"
version = "0.1.0"

[tool.commitizen]
name = "cz_conventional_commits"
version_provider = "pep621"
tag_format = "v$version"
major_version_zero = true

[tool.commitizen.change_type_map]
feat = "Added"
fix = "Fixed"
"""

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
        env={**_GIT_ENV, "PATH": _path()},
    )


def _path() -> str:
    import os

    return os.environ.get("PATH", "")


def _cz(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    import os

    return subprocess.run(
        [sys.executable, "-m", "commitizen", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_ENV},
    )


def _make_repo(tmp_path: Path, extra_commits: list[str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    _git(repo, "init", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "feat: seed the project")
    _git(repo, "tag", "v0.1.0")
    for i, msg in enumerate(extra_commits):
        (repo / f"f{i}.txt").write_text("x", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", msg)
    return repo


def test_feat_bumps_minor(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, ["feat: add a shiny feature"])
    out = _cz(repo, "bump", "--dry-run", "--yes")
    assert out.returncode == 0, out.stderr
    assert "0.2.0" in (out.stdout + out.stderr)


def test_fix_bumps_patch(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, ["fix: correct an off-by-one"])
    out = _cz(repo, "bump", "--dry-run", "--yes")
    assert out.returncode == 0, out.stderr
    assert "0.1.1" in (out.stdout + out.stderr)


def test_breaking_change_stays_in_zero_major(tmp_path: Path) -> None:
    # Under major_version_zero, a breaking change bumps the MINOR (0.x.0), never
    # jumps to 1.0.0.
    repo = _make_repo(tmp_path, ["feat!: drop the legacy field"])
    out = _cz(repo, "bump", "--dry-run", "--yes")
    combined = out.stdout + out.stderr
    assert out.returncode == 0, out.stderr
    assert "0.2.0" in combined
    assert "1.0.0" not in combined


def test_cz_check_accepts_and_rejects(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, [])
    ok_file = tmp_path / "ok.txt"
    ok_file.write_text("feat(api): add a route", encoding="utf-8")
    accepted = _cz(repo, "check", "--commit-msg-file", str(ok_file))
    assert accepted.returncode == 0, accepted.stderr

    bad_file = tmp_path / "bad.txt"
    bad_file.write_text("wip stuff", encoding="utf-8")
    rejected = _cz(repo, "check", "--commit-msg-file", str(bad_file))
    assert rejected.returncode != 0


def test_changelog_generation_is_grouped_and_idempotent(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, ["feat: add a feature", "fix: fix a bug"])
    first = _cz(repo, "changelog")
    assert first.returncode == 0, first.stderr
    changelog = repo / "CHANGELOG.md"
    assert changelog.is_file()
    content = changelog.read_text(encoding="utf-8")
    # Keep-a-Changelog groupings via change_type_map.
    assert "### Added" in content
    assert "### Fixed" in content
    assert "add a feature" in content

    # Regenerating over an unchanged range is idempotent.
    second = _cz(repo, "changelog")
    assert second.returncode == 0, second.stderr
    assert changelog.read_text(encoding="utf-8") == content
