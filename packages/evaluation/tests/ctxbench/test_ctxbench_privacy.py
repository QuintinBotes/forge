"""The subject's owner never appears in a ctxbench file (spec J §2.4.1).

The organisation that owns the benchmark's subject repository is a runtime
value (``FORGE_CTXBENCH_SUBJECT``), never source text. Skipped when the
variable is unset, as in a fork's CI.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
CTXBENCH_PATHS = (
    "packages/evaluation/forge_eval/ctxbench",
    "packages/evaluation/forge_eval/benchmarks/ctxbench-lore",
    "packages/evaluation/tests/ctxbench",
    "apps/api/forge_api/cli_ctxbench.py",
    "apps/api/tests/test_cli_ctxbench.py",
    "docs/CTXBENCH_RESULTS.md",
)


def _files() -> list[Path]:
    out: list[Path] = []
    for rel in CTXBENCH_PATHS:
        path = ROOT / rel
        out += [p for p in path.rglob("*") if p.is_file()] if path.is_dir() else [path]
    return [p for p in out if "__pycache__" not in p.parts]


@pytest.mark.skipif(
    not os.environ.get("FORGE_CTXBENCH_SUBJECT"), reason="FORGE_CTXBENCH_SUBJECT is unset"
)
def test_subject_owner_never_appears_in_ctxbench_files() -> None:
    owner = os.environ["FORGE_CTXBENCH_SUBJECT"].split("/", 1)[0].strip().casefold()
    assert owner
    files = _files()
    assert len(files) > 10
    leaks = [
        str(p.relative_to(ROOT))
        for p in files
        if owner in p.read_bytes().decode("utf-8", "replace").casefold()
    ]
    assert leaks == []
