"""End-to-end engine + CLI behaviour on a temp fake manifest (AC6, AC8).

Uses only evidence / manual / creds-gated gates so no heavy subprocess runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forge_eval.release.model import Bar, Status, bar_met, load_gates
from forge_eval.release.readiness import evaluate, main

_MANIFEST = """
gates:
  - id: G-EV
    bar: beta
    blocker: 6
    workstream: HARD-12
    title: an evidence gate that is present
    check:
      kind: evidence
      artifact: present.json
      predicate: { type: exists }
  - id: G-CREDS
    bar: beta
    blocker: 1
    workstream: HARD-02
    title: a creds-gated command gate
    check:
      kind: command
      run: "false"
      required_env: [FORGE_UNSET_CRED_ABC]
  - id: G-PENTEST
    bar: production
    blocker: 4
    workstream: external
    title: human-only
    check:
      kind: manual
      attestation: att.yaml
"""


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "release").mkdir()
    (tmp_path / "release" / "gates.yaml").write_text(_MANIFEST, encoding="utf-8")
    (tmp_path / "present.json").write_text("{}", encoding="utf-8")
    (tmp_path / "att.yaml").write_text("gate: G-PENTEST\nsigned_off: false\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "9.9.9"\n', encoding="utf-8")
    return tmp_path


def test_evaluate_resolves_each_kind(repo: Path) -> None:
    gates = load_gates(repo / "release" / "gates.yaml")
    results = {r.gate.id: r for r in evaluate(gates, root=repo)}
    assert results["G-EV"].status is Status.GREEN
    assert results["G-CREDS"].status is Status.SKIPPED_NO_CREDS
    assert results["G-PENTEST"].status is Status.MANUAL_PENDING


def test_beta_bar_met_when_only_creds_missing_is_false(repo: Path) -> None:
    gates = load_gates(repo / "release" / "gates.yaml")
    results = evaluate(gates, root=repo)
    # SKIPPED_NO_CREDS ⇒ beta NOT MET.
    assert bar_met(results, Bar.BETA) is False


def test_cli_check_exits_nonzero_when_not_met(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = repo / "RR.md"
    code = main(
        [
            "--bar",
            "production",
            "--manifest",
            str(repo / "release" / "gates.yaml"),
            "--root",
            str(repo),
            "--out",
            str(out),
            "--check",
        ]
    )
    assert code == 1
    assert out.exists()
    assert "NOT MET" in out.read_text(encoding="utf-8")


def test_cli_report_only_always_exits_zero(repo: Path) -> None:
    code = main(
        [
            "--bar",
            "production",
            "--manifest",
            str(repo / "release" / "gates.yaml"),
            "--root",
            str(repo),
            "--out",
            "-",
            "--report-only",
        ]
    )
    assert code == 0


def test_cli_only_subset_and_json(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        [
            "--bar",
            "production",
            "--manifest",
            str(repo / "release" / "gates.yaml"),
            "--root",
            str(repo),
            "--out",
            "-",
            "--only",
            "G-EV",
            "--json",
        ]
    )
    captured = capsys.readouterr()
    # --only G-EV ⇒ only the GREEN gate selected ⇒ bar MET ⇒ exit 0.
    assert code == 0
    payload = json.loads(captured.err)
    assert [g["id"] for g in payload["gates"]] == ["G-EV"]
    assert payload["met"] is True


def test_cli_uses_project_version_in_header(repo: Path) -> None:
    out = repo / "RR.md"
    main(
        [
            "--bar",
            "beta",
            "--manifest",
            str(repo / "release" / "gates.yaml"),
            "--root",
            str(repo),
            "--out",
            str(out),
            "--report-only",
        ]
    )
    assert "`9.9.9`" in out.read_text(encoding="utf-8")
