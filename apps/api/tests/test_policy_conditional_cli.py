"""F29 — ``forge-cli policy simulate`` / ``policy test`` (AC19)."""

from __future__ import annotations

from pathlib import Path

import pytest

from forge_api.cli import main

_EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "policies" / "conditional"
DEPLOY_GATED = _EXAMPLES / "deploy-time-gated.yaml"
INFRA_GATED = _EXAMPLES / "infra-branch-gated.yaml"


def test_cli_simulate_prints_matched_rule(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(
        [
            "policy",
            "simulate",
            str(INFRA_GATED),
            "--action",
            "write_file",
            "--path",
            "infra/x.tf",
            "--branch",
            "feature/x",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "effect=deny" in out
    assert "infra-writes-main-only" in out
    assert "base_effect: allow" in out


def test_cli_simulate_allows_in_window(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(
        [
            "policy",
            "simulate",
            str(DEPLOY_GATED),
            "--action",
            "deploy",
            "--env",
            "production",
            "--now",
            "2026-06-23T12:00:00Z",  # Tuesday noon -> in window -> allowed
        ]
    )
    assert rc == 0
    assert "effect=allow" in capsys.readouterr().out


def test_cli_test_exit_zero_on_pass(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["policy", "test", str(DEPLOY_GATED)])
    assert rc == 0
    assert "passed" in capsys.readouterr().out


def test_cli_test_exit_one_on_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A policy whose suite asserts the wrong outcome must exit non-zero.
    policy = tmp_path / "p.yaml"
    policy.write_text(INFRA_GATED.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "p.tests.yaml").write_text(
        "cases:\n"
        "  - name: infra write on feature branch (wrongly expects allow)\n"
        "    context: { branch: feature/x }\n"
        "    tool_call: { tool: write_file, path: infra/x.tf }\n"
        "    expect_effect: allow\n",
        encoding="utf-8",
    )
    rc = main(["policy", "test", str(policy)])
    assert rc == 1
    out = capsys.readouterr().out
    assert "FAIL" in out
