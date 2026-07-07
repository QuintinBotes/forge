"""F35 CLI tests — `forge bench` freeze/hash/verify exit codes (AC21/AC24)."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from forge_api.cli_bench import main
from forge_eval.benchmark import (
    BenchmarkScoring,
    compute_benchmark_score,
    load_manifest,
    make_bundle,
    replay_bundles,
)
from forge_eval.golden import GoldenCase

SCORING = {
    "primary_metric": "benchmark.composite",
    "metric_weights": {"retrieval.recall_at_k": 0.7, "retrieval.mrr": 0.3},
    "category_field": "tags",
    "k": 5,
}


def _write_suite(root: Path, *, terminal_state: str = "pr_opened") -> Path:
    suite_dir = root / "demo" / "1.0.0"
    (suite_dir / "cases").mkdir(parents=True)
    (suite_dir / "cases" / "all.yaml").write_text(
        yaml.safe_dump(
            {
                "cases": [
                    {
                        "id": "c1",
                        "query": "q1",
                        "expected_ids": ["a", "b"],
                        "kind": "retrieval",
                        "tags": ["retrieval"],
                    },
                    {
                        "id": "t1",
                        "query": "do task",
                        "expected_ids": ["REQ-1"],
                        "kind": "agent_task",
                        "tags": ["agent_task"],
                        "metadata": {"expected_terminal_state": terminal_state},
                    },
                ]
            }
        )
    )
    (suite_dir / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "slug": "demo",
                "version": "1.0.0",
                "title": "Demo",
                "description": "",
                "schema_version": 1,
                "frozen": False,
                "scoring": SCORING,
                "case_files": ["cases/all.yaml"],
            }
        )
    )
    return suite_dir


def test_bench_freeze_writes_hash_and_is_idempotent(tmp_path: Path) -> None:
    suite_dir = _write_suite(tmp_path)
    assert main(["freeze", str(suite_dir)]) == 0
    manifest, _cases = load_manifest(suite_dir)
    assert manifest.frozen is True
    assert manifest.content_hash and manifest.content_hash.startswith("sha256:")
    # Refreeze over unchanged cases: same hash, still exit 0.
    assert main(["freeze", str(suite_dir)]) == 0


def test_bench_freeze_exit_1_on_drift(tmp_path: Path) -> None:
    """AC21: post-freeze content drift exits 1."""
    suite_dir = _write_suite(tmp_path)
    assert main(["freeze", str(suite_dir)]) == 0
    case_file = suite_dir / "cases" / "all.yaml"
    drifted = case_file.read_text().replace('"a"', '"drifted"').replace("- a", "- drifted")
    case_file.write_text(drifted)
    assert main(["freeze", str(suite_dir)]) == 1
    assert main(["hash", str(suite_dir)]) == 1


def test_bench_freeze_rejects_merged_terminal_state(tmp_path: Path) -> None:
    """AC24: freeze exits 1 when an agent_task case declares merged."""
    suite_dir = _write_suite(tmp_path, terminal_state="merged")
    assert main(["freeze", str(suite_dir)]) == 1


def _submission_file(tmp_path: Path, suite_dir: Path, *, tamper: bool = False) -> Path:
    manifest, cases = load_manifest(suite_dir)
    bundles = [make_bundle("c1", ["a", "b"]), make_bundle("t1", ["REQ-1"])]
    scoring = manifest.scoring
    report = replay_bundles(bundles, cases, scoring)
    claimed = compute_benchmark_score(report, scoring, cases)
    if tamper:
        claimed = claimed.model_copy(update={"composite": 0.123456})
    path = tmp_path / "submission.json"
    path.write_text(
        json.dumps(
            {
                "claimed": claimed.model_dump(mode="json"),
                "claimed_bundle_hashes": [b.content_hash for b in bundles],
                "bundles": [b.model_dump(mode="json") for b in bundles],
            }
        )
    )
    return path


def test_bench_verify_exit_codes(tmp_path: Path) -> None:
    """AC21: verify exits 0 (verified) / 1 (rejected)."""
    suite_dir = _write_suite(tmp_path)
    assert main(["freeze", str(suite_dir)]) == 0

    faithful = _submission_file(tmp_path, suite_dir)
    assert main(["verify", "--suite-dir", str(suite_dir), "--submission", str(faithful)]) == 0

    tampered = _submission_file(tmp_path, suite_dir, tamper=True)
    assert main(["verify", "--suite-dir", str(suite_dir), "--submission", str(tampered)]) == 1


def test_bench_verify_missing_suite_dir_errors(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    submission = tmp_path / "s.json"
    submission.write_text("{}")
    assert main(["verify", "--suite-dir", str(missing), "--submission", str(submission)]) == 1


def test_scoring_helper_types_roundtrip() -> None:
    # Guard the fixture scoring against schema drift.
    scoring = BenchmarkScoring.model_validate(SCORING)
    case = GoldenCase(id="c1", query="q", expected_ids=["a"])
    assert scoring.k == 5
    assert case.kind == "retrieval"
