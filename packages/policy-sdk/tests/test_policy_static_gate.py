"""F40-POL-GOVERNANCE — the static forbidden-shortcuts scanner."""

from __future__ import annotations

from forge_policy import ShortcutViolation, StaticGateResult, scan_forbidden_shortcuts


def test_clean_files_pass() -> None:
    files = {"app/main.py": "def main():\n    return run_all_tests()\n"}
    result = scan_forbidden_shortcuts(files, ["# type: ignore", "skip failing tests"])
    assert result.passed is True
    assert result.violations == []
    assert "passed" in result.summary


def test_forbidden_shortcut_fails_and_locates_violation() -> None:
    files = {
        "app/a.py": "x = 1  # type: ignore\n",
        "app/b.py": "ok = True\n",
    }
    result = scan_forbidden_shortcuts(files, ["# type: ignore"])
    assert result.passed is False
    assert len(result.violations) == 1
    v = result.violations[0]
    assert isinstance(v, ShortcutViolation)
    assert v.file == "app/a.py"
    assert v.line == 1
    assert v.shortcut == "# type: ignore"
    assert "failed" in result.summary


def test_match_is_case_insensitive() -> None:
    files = {"a.txt": "We should SKIP FAILING TESTS here\n"}
    result = scan_forbidden_shortcuts(files, ["skip failing tests"])
    assert result.passed is False


def test_no_shortcuts_declared_always_passes() -> None:
    files = {"a.txt": "# type: ignore\n"}
    result = scan_forbidden_shortcuts(files, [])
    assert result.passed is True


def test_multiple_violations_across_files_sorted() -> None:
    files = {
        "z.py": "eslint-disable\n",
        "a.py": "line1\neslint-disable line2\n",
    }
    result = scan_forbidden_shortcuts(files, ["eslint-disable"])
    assert result.passed is False
    assert [(v.file, v.line) for v in result.violations] == [("a.py", 2), ("z.py", 1)]


def test_result_model_defaults() -> None:
    result = StaticGateResult()
    assert result.passed is True
    assert result.violations == []
