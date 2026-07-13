"""F41 Self-Eval Gate worker task: append minted cases + re-freeze (offline).

No git, no GitHub, no real provider: a scripted in-memory ``TestRunner`` + fake
PR source drive the mint, and the suite lives in a ``tmp_path`` dir. Asserts the
private suite grows, the manifest re-freezes with a fresh content hash, and the
minted hidden tests round-trip through ``load_manifest`` (so a gate run can load
them from disk).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from forge_eval.benchmark import load_manifest, parse_swe_case_fields
from forge_eval.benchmark.manifest import compute_content_hash, freeze
from forge_eval.golden import GoldenCase
from forge_eval.mint import ChangedFile
from forge_worker.celery_app import celery_app
from forge_worker.tasks.self_eval_mint import (
    MINTED_CASE_FILE,
    append_minted_cases,
    mint_and_store,
)


@dataclass
class FakePR:
    number: int
    head_sha: str
    title: str = "Fix regression"


class FakeSource:
    def __init__(self, files_by_pr: dict[int, list[ChangedFile]], base: str) -> None:
        self._files = files_by_pr
        self._base = base

    def pr_changed_files(self, repo: str, number: int) -> list[ChangedFile]:
        return list(self._files.get(number, []))

    def pr_base_commit(self, repo: str, number: int) -> str:
        return self._base


class ScriptedRunner:
    """Deterministic in-memory TestRunner: ``outcomes[(ref, node)] -> passed``."""

    def __init__(self, outcomes: dict[tuple[str, str], bool], collected: list[str]) -> None:
        self._outcomes = outcomes
        self._collected = collected

    def run_tests(self, *, ref: str, node_ids: Sequence[str]) -> dict[str, bool]:
        return {nid: self._outcomes.get((ref, nid), False) for nid in node_ids}

    def collect_tests(self, *, ref: str) -> list[str]:
        return list(self._collected)


_PATCH = "@@ -0,0 +1,2 @@\n+def test_new():\n+    assert True\n"


def _seed_suite(version_dir: Path) -> None:
    """Write a valid, frozen 1-case seed private suite."""
    version_dir.mkdir(parents=True, exist_ok=True)
    (version_dir / "cases").mkdir(exist_ok=True)
    seed = [GoldenCase(id="seed-1", query="seed", expected_ids=["a"], tags=["seed"])]
    (version_dir / "cases" / "seed.json").write_text(
        json.dumps([{"id": "seed-1", "query": "seed", "expected_ids": ["a"], "tags": ["seed"]}]),
        encoding="utf-8",
    )
    scoring = {
        "primary_metric": "benchmark.composite",
        "metric_weights": {"retrieval.recall_at_k": 1.0},
    }
    from forge_eval.benchmark import BenchmarkManifest

    manifest = BenchmarkManifest.model_validate(
        {
            "slug": "acme-private",
            "version": "1.0.0",
            "title": "acme private suite",
            "scoring": scoring,
            "case_files": ["cases/seed.json"],
        }
    )
    frozen = freeze(manifest, seed)
    (version_dir / "manifest.yaml").write_text(
        yaml.safe_dump(frozen.model_dump(mode="json"), sort_keys=False), encoding="utf-8"
    )


def test_append_minted_cases_grows_and_refreezes(tmp_path: Path) -> None:
    version_dir = tmp_path / "suite" / "1.0.0"
    _seed_suite(version_dir)
    before, _ = load_manifest(version_dir)

    minted = GoldenCase(
        id="self-eval-acme-pr-1",
        query="Fix the bug",
        expected_ids=["test_new.py::test_new"],
        kind="agent_task",
        tags=["self-eval"],
        metadata={
            "fail_to_pass": ["test_new.py::test_new"],
            "pass_to_pass": ["test_old.py::test_old"],
            "sandbox_image": "python:3.14-slim",
            "setup_commands": ["pip install -e ."],
            "base_commit": "deadbeef",
            "expected_terminal_state": "pr_opened",
        },
    )
    added = append_minted_cases(version_dir, [minted])
    assert added == 1

    after, cases = load_manifest(version_dir)
    assert after.frozen is True
    assert after.content_hash != before.content_hash  # re-froze with a new hash
    assert MINTED_CASE_FILE in after.case_files
    ids = {c.id for c in cases}
    assert ids == {"seed-1", "self-eval-acme-pr-1"}

    # Hidden test wiring survives the disk round-trip.
    minted_loaded = next(c for c in cases if c.id == "self-eval-acme-pr-1")
    fields = parse_swe_case_fields(minted_loaded)
    assert fields.fail_to_pass == ["test_new.py::test_new"]
    assert fields.pass_to_pass == ["test_old.py::test_old"]


def test_append_minted_cases_is_idempotent_on_id(tmp_path: Path) -> None:
    version_dir = tmp_path / "suite" / "1.0.0"
    _seed_suite(version_dir)
    case = GoldenCase(
        id="self-eval-acme-pr-2",
        query="q",
        expected_ids=["t::t"],
        kind="agent_task",
        tags=["self-eval"],
        metadata={"fail_to_pass": ["t::t"], "expected_terminal_state": "pr_opened"},
    )
    assert append_minted_cases(version_dir, [case]) == 1
    # Re-appending the same id adds nothing new and still re-freezes cleanly.
    assert append_minted_cases(version_dir, [case]) == 0
    _, cases = load_manifest(version_dir)
    assert sum(1 for c in cases if c.id == "self-eval-acme-pr-2") == 1


def test_mint_and_store_end_to_end(tmp_path: Path) -> None:
    version_dir = tmp_path / "suite" / "1.0.0"
    _seed_suite(version_dir)

    node = "test_new.py::test_new"
    changed = [ChangedFile(path="test_new.py", status="added", patch=_PATCH)]
    source = FakeSource({1: changed}, "base1")
    runner = ScriptedRunner(
        outcomes={
            ("base1", node): False,  # fails before
            ("head1", node): True,  # passes after
            ("base1", "test_old.py::test_old"): True,
            ("head1", "test_old.py::test_old"): True,
        },
        collected=["test_old.py::test_old", node],
    )
    pr = FakePR(number=1, head_sha="head1")

    minted_ids = mint_and_store(
        [pr], "acme/widgets", version_dir, source=source, runner=runner, sandbox_image="img"
    )
    assert minted_ids == ["self-eval-acme-widgets-pr-1"]

    _, cases = load_manifest(version_dir)
    minted = next(c for c in cases if c.id == "self-eval-acme-widgets-pr-1")
    fields = parse_swe_case_fields(minted)
    assert fields.fail_to_pass == [node]
    assert fields.pass_to_pass == ["test_old.py::test_old"]
    assert fields.sandbox_image == "img"


def test_content_hash_matches_manual_recompute(tmp_path: Path) -> None:
    version_dir = tmp_path / "suite" / "1.0.0"
    _seed_suite(version_dir)
    case = GoldenCase(
        id="self-eval-acme-pr-3",
        query="q",
        expected_ids=["t::t"],
        kind="agent_task",
        tags=["self-eval"],
        metadata={"fail_to_pass": ["t::t"], "expected_terminal_state": "pr_opened"},
    )
    append_minted_cases(version_dir, [case])
    manifest, cases = load_manifest(version_dir)
    assert manifest.content_hash == compute_content_hash(cases, manifest.scoring)


def test_task_registered() -> None:
    assert "forge.self_eval.mint" in celery_app.tasks
