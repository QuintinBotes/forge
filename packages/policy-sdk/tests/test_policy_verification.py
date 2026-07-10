"""F40-POL-GOVERNANCE — the automatic static-gate verification service.

Proves ``forbidden_shortcuts`` is a *check that fails* over produced files, not
only an on-demand endpoint: the verifier collects a worktree's files and turns a
forbidden shortcut into a failing named check.
"""

from __future__ import annotations

from pathlib import Path

from forge_policy import STATIC_GATE_CHECK, collect_files, run_static_gate, static_gate_check

_FORBIDDEN = ["# type: ignore", "skip failing tests"]


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def test_clean_worktree_passes(tmp_path: Path) -> None:
    _write(tmp_path, "src/app.py", "def main() -> None:\n    return None\n")
    result = run_static_gate(tmp_path, _FORBIDDEN)
    assert result.passed is True
    assert static_gate_check(result) == {STATIC_GATE_CHECK: True}


def test_forbidden_shortcut_fails_the_check(tmp_path: Path) -> None:
    _write(tmp_path, "src/app.py", "x = 1  # type: ignore\n")
    result = run_static_gate(tmp_path, _FORBIDDEN)
    assert result.passed is False
    assert static_gate_check(result) == {STATIC_GATE_CHECK: False}
    assert result.violations[0].file == "src/app.py"
    assert result.violations[0].line == 1


def test_collect_skips_vcs_and_build_dirs(tmp_path: Path) -> None:
    _write(tmp_path, "src/app.py", "ok\n")
    _write(tmp_path, ".git/config", "# type: ignore\n")
    _write(tmp_path, "node_modules/x/index.js", "skip failing tests\n")
    files = collect_files(tmp_path)
    assert set(files) == {"src/app.py"}
    # And so the scan ignores shortcuts buried in skipped trees.
    assert run_static_gate(tmp_path, _FORBIDDEN).passed is True


def test_paths_restricts_scan_to_change_set(tmp_path: Path) -> None:
    _write(tmp_path, "src/new.py", "y = 2  # type: ignore\n")
    _write(tmp_path, "src/old.py", "z = 3  # type: ignore\n")
    # Only the changed file is scanned; the pre-existing violation is ignored.
    result = run_static_gate(tmp_path, _FORBIDDEN, paths=["src/new.py"])
    assert result.passed is False
    assert {v.file for v in result.violations} == {"src/new.py"}
