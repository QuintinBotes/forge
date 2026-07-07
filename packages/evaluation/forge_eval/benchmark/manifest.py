"""Benchmark manifest loading, canonical content hashing, and freezing (F35).

The ``content_hash`` is the reproducibility anchor: sha256 over the canonical
JSON of the *sorted* case set plus the scoring rubric. It is order-independent
of the file listing and stable across processes, so a frozen suite version is
bit-comparable forever — mutating any case body after freeze is detected and
rejected (bump ``version`` instead).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from forge_eval.benchmark.errors import (
    BenchmarkContentHashMismatch,
    BenchmarkFrozenError,
)
from forge_eval.benchmark.models import BenchmarkManifest, BenchmarkScoring
from forge_eval.golden import GoldenCase, load_golden_set

__all__ = [
    "MANIFEST_FILENAME",
    "compute_content_hash",
    "freeze",
    "load_manifest",
    "validate_freezable",
]

MANIFEST_FILENAME = "manifest.yaml"

#: Terminal state a benchmark ``agent_task`` case must never declare (AC24 —
#: human approval before merge is preserved: the harness never merges a PR).
_FORBIDDEN_TERMINAL_STATE = "merged"


def _canonical_case(case: GoldenCase) -> dict[str, Any]:
    return asdict(case)


def compute_content_hash(cases: Sequence[GoldenCase], scoring: BenchmarkScoring) -> str:
    """sha256 over canonical JSON of the sorted cases + the scoring rubric.

    Order-independent of the input case ordering (cases are sorted by id) and
    stable across processes (sorted keys, compact separators).
    """
    payload = {
        "cases": sorted((_canonical_case(case) for case in cases), key=lambda c: str(c["id"])),
        "scoring": scoring.model_dump(mode="json"),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_freezable(cases: Sequence[GoldenCase]) -> None:
    """Reject suites that would violate the merge non-negotiable (AC24)."""
    for case in cases:
        if (
            case.kind == "agent_task"
            and str(case.metadata.get("expected_terminal_state", "")) == _FORBIDDEN_TERMINAL_STATE
        ):
            raise BenchmarkFrozenError(
                f"case {case.id!r} declares expected_terminal_state=merged; benchmark "
                "agent_task cases must terminate before merge (human approval gate)"
            )


def freeze(manifest: BenchmarkManifest, cases: Sequence[GoldenCase]) -> BenchmarkManifest:
    """Return a frozen copy of ``manifest`` with its ``content_hash`` computed.

    Raises :class:`BenchmarkFrozenError` if the manifest is already frozen and
    the recomputed hash differs (the task set drifted — bump ``version``), or if
    any ``agent_task`` case declares a merge terminal state (AC24).
    """
    validate_freezable(cases)
    recomputed = compute_content_hash(cases, manifest.scoring)
    if manifest.frozen and manifest.content_hash and manifest.content_hash != recomputed:
        raise BenchmarkFrozenError(
            f"suite {manifest.slug}@{manifest.version} is frozen with "
            f"{manifest.content_hash} but its cases now hash to {recomputed}; "
            "content_hash mismatch; bump version"
        )
    return manifest.model_copy(update={"content_hash": recomputed, "frozen": True})


def _load_manifest_file(version_dir: Path) -> BenchmarkManifest:
    manifest_path = version_dir / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise FileNotFoundError(f"benchmark manifest not found: {manifest_path}")
    try:
        import yaml  # lazy: mirrors forge_eval.golden's optional YAML dependency
    except ModuleNotFoundError as exc:  # pragma: no cover - env without PyYAML
        raise RuntimeError("PyYAML is required to load benchmark manifests") from exc
    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"benchmark manifest must be a mapping: {manifest_path}")
    return BenchmarkManifest.model_validate(raw)


def load_manifest(version_dir: str | Path) -> tuple[BenchmarkManifest, list[GoldenCase]]:
    """Load ``manifest.yaml`` + every referenced case file under ``version_dir``.

    If the manifest is frozen, the recomputed content hash must equal the
    manifest's ``content_hash``; otherwise :class:`BenchmarkContentHashMismatch`
    is raised (the only resolution is a version bump).
    """
    resolved = Path(version_dir)
    manifest = _load_manifest_file(resolved)

    cases: list[GoldenCase] = []
    seen: set[str] = set()
    for rel in manifest.case_files:
        for case in load_golden_set(resolved / rel):
            if case.id in seen:
                raise ValueError(f"duplicate benchmark case id across files: {case.id!r}")
            seen.add(case.id)
            cases.append(case)

    if manifest.frozen:
        recomputed = compute_content_hash(cases, manifest.scoring)
        if recomputed != manifest.content_hash:
            raise BenchmarkContentHashMismatch(
                f"suite {manifest.slug}@{manifest.version}: cases hash to {recomputed} "
                f"but the frozen manifest declares {manifest.content_hash}; "
                "content_hash mismatch; bump version"
            )
    return manifest, cases
