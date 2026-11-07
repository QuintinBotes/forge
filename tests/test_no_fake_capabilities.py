"""Production code must not fabricate results (ADR-0007).

``example.invalid`` was the URL of a PR that never existed, returned by the
Temporal stand-in on production paths. It may appear in tests, never in shipped
code.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = "example.invalid"


def _shipped_python_files() -> list[Path]:
    files: list[Path] = []
    for top in ("apps", "packages"):
        for path in (ROOT / top).rglob("*.py"):
            parts = set(path.relative_to(ROOT).parts)
            if parts & {"tests", "node_modules", ".venv", "__pycache__"}:
                continue
            files.append(path)
    return files


def test_no_shipped_code_mentions_a_fabricated_pr_url() -> None:
    files = _shipped_python_files()
    assert files, "scanned no files; the repository layout changed"
    offenders = [
        str(p.relative_to(ROOT)) for p in files if FORBIDDEN in p.read_text(errors="ignore")
    ]
    assert offenders == []
