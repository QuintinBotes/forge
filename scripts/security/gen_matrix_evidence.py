#!/usr/bin/env python3
"""Render security/enforcement-matrix.yaml to the committed evidence markdown.

Usage: uv run python scripts/security/gen_matrix_evidence.py

The regression suite (tests/security/test_enforcement_matrix.py) asserts the
rendering is byte-for-byte in sync with the YAML, so edit the YAML and re-run
this script — never hand-edit the markdown.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MATRIX = REPO_ROOT / "security" / "enforcement-matrix.yaml"
EVIDENCE = REPO_ROOT / "docs" / "security" / "evidence" / "enforcement-matrix.md"

HEADER = """\
# Security Enforcement Matrix — evidence rendering

> GENERATED from [`security/enforcement-matrix.yaml`](../../../security/enforcement-matrix.yaml)
> by `scripts/security/gen_matrix_evidence.py` — do not edit by hand.
> Every row is asserted on the wired request path by
> `tests/security/test_enforcement_matrix.py` (`uv run pytest -m security -q`).
> `offline` rows run hermetically; `live-db` rows need `FORGE_TEST_DATABASE_URL`
> (pgvector) and skip cleanly without it.

| Control | Title | Mode | Spec source | Verified by |
|---|---|---|---|---|
"""


def render(controls: list[dict[str, Any]]) -> str:
    lines = [HEADER]
    for row in controls:
        spec = str(row["spec"]).replace("|", "\\|").strip()
        lines.append(
            f"| `{row['id']}` | {row['title'].strip()} | {row['mode']} | {spec} "
            f"| `{row['check']}` |\n"
        )
    lines.append("\n## What each row asserts\n\n")
    for row in controls:
        asserts = " ".join(str(row["asserts"]).split())
        lines.append(f"### `{row['id']}`\n\n{asserts}\n\n")
    return "".join(lines)


def main() -> int:
    controls = yaml.safe_load(MATRIX.read_text())["controls"]
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(render(controls))
    print(f"wrote {EVIDENCE.relative_to(REPO_ROOT)} ({len(controls)} controls)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
