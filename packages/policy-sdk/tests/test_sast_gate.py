"""#187 criterion 5: the SAST gate runs bandit (a dev dependency) over the changed files."""

from __future__ import annotations

from pathlib import Path

import pydantic
import pytest

from scf_contracts import Policy
from scf_policy.loader import load_policy
from scf_policy.sast_gate import run_sast

SHELL = (
    "import subprocess\n\n\ndef run(command):\n    return subprocess.Popen(command, shell=True)\n"
)
YAML_LOAD = "import yaml\n\n\ndef read(text):\n    return yaml.load(text)\n"


def test_a_finding_at_the_configured_severity_fails_with_file_line_and_rule() -> None:
    result = run_sast({"app/run.py": SHELL}, severity="high")

    assert result.status == "failed"
    [finding] = result.findings
    assert (finding.file, finding.line, finding.rule) == ("app/run.py", 5, "B602")


def test_a_nosec_comment_in_the_change_does_not_silence_the_analyser() -> None:
    silenced = SHELL.replace("shell=True)", "shell=True)  # nosec")

    assert [f.rule for f in run_sast({"app/run.py": silenced}).findings] == ["B602"]


def test_a_finding_below_the_configured_severity_passes() -> None:
    files = {"app/config.py": YAML_LOAD}  # B506 is medium

    assert run_sast(files, severity="high").passed
    assert [f.rule for f in run_sast(files, severity="medium").findings] == ["B506"]


def test_only_the_changed_files_the_analyser_reads_are_analysed() -> None:
    result = run_sast({"app/clean.py": "VALUE = 1\n", "web/app.ts": "eval(input)\n"})

    assert result.passed and result.findings == []


def test_an_analyser_that_is_not_installed_cannot_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))  # an empty PATH: no bandit

    result = run_sast({"app/run.py": SHELL})

    assert result.status == "could_not_verify" and not result.passed
    # the worker's fault, not the change's: a person fixes it, not the builder
    assert (result.unavailable, result.reasons) == (["bandit is not installed on this worker"], [])


def test_a_change_with_no_file_to_analyse_needs_no_analyser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))

    assert run_sast({"README.md": "hello\n"}).passed


def test_a_file_the_analyser_cannot_read_cannot_verify() -> None:
    result = run_sast({"app/broken.py": "def broken(:\n"})

    assert result.status == "could_not_verify"
    assert result.reasons == ["app/broken.py: bandit could not read it"]


def test_a_changed_file_that_could_not_be_read_cannot_verify() -> None:
    result = run_sast({"app/run.py": None})

    assert result.status == "could_not_verify"
    assert result.reasons == ["app/run.py: not read as a regular file"]


def test_a_path_bandit_excludes_by_default_is_still_analysed() -> None:
    result = run_sast({".github/scripts/ci.py": SHELL, "tools/CVSsync.py": SHELL})

    assert sorted(f.file for f in result.findings) == [".github/scripts/ci.py", "tools/CVSsync.py"]


def test_a_file_the_analyser_did_not_report_on_cannot_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "bandit"
    fake.write_text('#!/bin/sh\necho \'{"errors": [], "results": [], "metrics": {}}\'\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    result = run_sast({"app/run.py": SHELL})

    assert result.status == "could_not_verify"
    assert result.unavailable == ["app/run.py: bandit did not analyse it"]


def test_the_severity_and_analyser_are_policy_fields_with_a_default(tmp_path: Path) -> None:
    default = Policy(repo_id="r").diff_gates
    assert (default.sast_analyzer, default.sast_severity) == ("bandit", "high")
    (tmp_path / "policy.yaml").write_text("repo_id: r\ndiff_gates:\n  sast_severity: medium\n")
    assert load_policy(tmp_path / "policy.yaml").diff_gates.sast_severity == "medium"


@pytest.mark.parametrize(
    "block", ["sast_severity: critical", "sast_analyzer: semgrep", "sast_severities: low"]
)
def test_an_invalid_gate_block_is_refused_when_the_policy_loads(tmp_path: Path, block: str) -> None:
    (tmp_path / "policy.yaml").write_text(f"repo_id: r\ndiff_gates:\n  {block}\n")

    with pytest.raises(pydantic.ValidationError):
        load_policy(tmp_path / "policy.yaml")
