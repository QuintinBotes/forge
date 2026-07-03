"""F35 unit tests — content hash, freeze, load (AC1/AC2/AC24)."""

from __future__ import annotations

import random
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from forge_eval.benchmark import (
    BenchmarkContentHashMismatch,
    BenchmarkFrozenError,
    BenchmarkManifest,
    BenchmarkScoring,
    compute_content_hash,
    freeze,
    load_manifest,
)
from forge_eval.golden import GoldenCase

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "benchmarks"
FIXTURE_SUITE = FIXTURE_DIR / "fixture" / "1.0.0"
SHIPPED_SUITE = (
    Path(__file__).resolve().parents[2] / "forge_eval" / "benchmarks" / "forge-swe" / "1.0.0"
)


def _scoring() -> BenchmarkScoring:
    return BenchmarkScoring(
        metric_weights={"retrieval.recall_at_k": 0.7, "retrieval.mrr": 0.3}
    )


def _cases() -> list[GoldenCase]:
    return [
        GoldenCase(id="b", query="q-b", expected_ids=["x"], tags=["retrieval"]),
        GoldenCase(id="a", query="q-a", expected_ids=["y", "z"], tags=["retrieval"]),
        GoldenCase(id="c", query="q-c", expected_ids=["w"], tags=["spec"]),
    ]


def test_content_hash_order_independent() -> None:
    cases = _cases()
    shuffled = list(cases)
    random.Random(7).shuffle(shuffled)
    assert compute_content_hash(cases, _scoring()) == compute_content_hash(
        shuffled, _scoring()
    )


def test_content_hash_changes_with_case_body_and_scoring() -> None:
    cases = _cases()
    base = compute_content_hash(cases, _scoring())
    mutated = [
        GoldenCase(id=c.id, query=c.query + "!", expected_ids=c.expected_ids) for c in cases
    ]
    assert compute_content_hash(mutated, _scoring()) != base
    other_scoring = BenchmarkScoring(metric_weights={"retrieval.recall_at_k": 1.0})
    assert compute_content_hash(cases, other_scoring) != base


def test_content_hash_stable_across_process() -> None:
    """AC1: the hash is identical when computed in a separate interpreter."""
    in_process = compute_content_hash(_cases(), _scoring())
    script = textwrap.dedent(
        """
        from forge_eval.benchmark import BenchmarkScoring, compute_content_hash
        from forge_eval.golden import GoldenCase
        cases = [
            GoldenCase(id="b", query="q-b", expected_ids=["x"], tags=["retrieval"]),
            GoldenCase(id="a", query="q-a", expected_ids=["y", "z"], tags=["retrieval"]),
            GoldenCase(id="c", query="q-c", expected_ids=["w"], tags=["spec"]),
        ]
        scoring = BenchmarkScoring(
            metric_weights={"retrieval.recall_at_k": 0.7, "retrieval.mrr": 0.3}
        )
        print(compute_content_hash(cases, scoring), end="")
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert out.stdout == in_process


def test_freeze_sets_hash_and_frozen() -> None:
    manifest = BenchmarkManifest(
        slug="demo",
        version="1.0.0",
        title="Demo",
        scoring=_scoring(),
        case_files=["cases.yaml"],
    )
    frozen = freeze(manifest, _cases())
    assert frozen.frozen is True
    assert frozen.content_hash == compute_content_hash(_cases(), _scoring())
    # Original manifest is untouched (freeze returns a copy).
    assert manifest.frozen is False


def test_freeze_then_mutation_raises_mismatch() -> None:
    """AC2: refreezing a frozen manifest over drifted cases is rejected."""
    manifest = BenchmarkManifest(
        slug="demo",
        version="1.0.0",
        title="Demo",
        scoring=_scoring(),
        case_files=["cases.yaml"],
    )
    frozen = freeze(manifest, _cases())
    drifted = _cases()
    drifted[0].expected_ids.append("extra")
    with pytest.raises(BenchmarkFrozenError, match="bump version"):
        freeze(frozen, drifted)


def test_freeze_rejects_merged_terminal_state() -> None:
    """AC24: an agent_task case may never terminate at `merged`."""
    manifest = BenchmarkManifest(
        slug="demo",
        version="1.0.0",
        title="Demo",
        scoring=_scoring(),
        case_files=["cases.yaml"],
    )
    bad = [
        *_cases(),
        GoldenCase(
            id="task-merge",
            query="do it",
            expected_ids=["REQ-1"],
            kind="agent_task",
            metadata={"expected_terminal_state": "merged"},
        ),
    ]
    with pytest.raises(BenchmarkFrozenError, match="merged"):
        freeze(manifest, bad)


def test_load_manifest_fixture_roundtrip() -> None:
    manifest, cases = load_manifest(FIXTURE_SUITE)
    assert manifest.frozen is True
    assert manifest.slug == "fixture"
    assert len(cases) == 3
    assert manifest.content_hash == compute_content_hash(cases, manifest.scoring)


def test_load_manifest_detects_on_disk_drift(tmp_path: Path) -> None:
    """AC2: mutating a case file after freeze raises BenchmarkContentHashMismatch."""
    suite = tmp_path / "fixture" / "1.0.0"
    shutil.copytree(FIXTURE_SUITE, suite)
    case_file = suite / "cases" / "retrieval.yaml"
    case_file.write_text(
        case_file.read_text().replace("auth.py::refresh", "auth.py::tampered")
    )
    with pytest.raises(BenchmarkContentHashMismatch, match="bump version"):
        load_manifest(suite)


def test_shipped_forge_swe_suite_is_frozen_and_valid() -> None:
    """The first public benchmark ships frozen with a verifying hash."""
    manifest, cases = load_manifest(SHIPPED_SUITE)
    assert manifest.slug == "forge-swe"
    assert manifest.frozen is True
    assert len(cases) == 5
    # AC24 holds for the shipped suite.
    for case in cases:
        if case.kind == "agent_task":
            assert case.metadata["expected_terminal_state"] != "merged"
