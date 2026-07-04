"""A real "docs lint" for the self-hosting guides (Task 1.17 gate).

There is no markdown linter in the offline sandbox, so this enforces the
structural invariants a linter would: every required guide exists and is
non-empty, starts with a single H1, balances its fenced code blocks, uses no
hard tabs, ends with a trailing newline, and every repo-relative link it makes
resolves to a file that actually exists. Headings and links inside fenced code
blocks are ignored (shell comments are not headings), so the lint matches how a
markdown renderer actually reads the document.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_ROOT = REPO_ROOT / "examples"
DOCS_DIR = REPO_ROOT / "docs" / "self-hosting"

REQUIRED_DOCS = [
    "quickstart.md",
    "docker-compose.md",
    "kubernetes.md",
    "backup.md",
    "restore.md",
    "upgrade.md",
    "security.md",
    "troubleshooting.md",
]

# F24: the Kubernetes guide must document a SUPPORTED chart (no longer a preview)
# and keep F15's `must_reference` strings.
KUBERNETES_MUST_REFERENCE = ["deploy/helm", "helm install", "helm upgrade"]

# Inline markdown links: [text](target) — capture the target.
_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def _all_markdown() -> list[Path]:
    docs = [DOCS_DIR / name for name in REQUIRED_DOCS]
    return [*docs, EXAMPLES_ROOT / "README.md"]


def _non_fenced_lines(text: str) -> Iterator[str]:
    """Yield only the lines that live outside fenced (```) code blocks."""
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            yield line


def test_all_required_selfhosting_docs_exist() -> None:
    missing = [name for name in REQUIRED_DOCS if not (DOCS_DIR / name).is_file()]
    assert not missing, f"missing self-hosting docs: {missing}"


@pytest.mark.parametrize("path", _all_markdown(), ids=lambda p: p.name)
def test_doc_is_well_formed(path: Path) -> None:
    assert path.is_file(), f"missing doc: {path}"
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"{path.name} is empty"

    lines = text.splitlines()
    assert lines[0].startswith("# "), f"{path.name} must start with a single H1 heading"

    body = list(_non_fenced_lines(text))
    h1_count = sum(1 for line in body if line.startswith("# "))
    assert h1_count == 1, f"{path.name} must have exactly one H1 (found {h1_count})"

    fences = sum(1 for line in lines if line.lstrip().startswith("```"))
    assert fences % 2 == 0, f"{path.name} has an unbalanced code fence"

    assert "\t" not in text, f"{path.name} contains a hard tab (use spaces)"
    assert text.endswith("\n"), f"{path.name} must end with a trailing newline"


@pytest.mark.parametrize("path", _all_markdown(), ids=lambda p: p.name)
def test_repo_relative_links_resolve(path: Path) -> None:
    for line in _non_fenced_lines(path.read_text(encoding="utf-8")):
        for target in _LINK_RE.findall(line):
            link = target.split("#", 1)[0].strip()
            if not link or link.startswith(("http://", "https://", "mailto:")):
                continue
            resolved = (path.parent / link).resolve()
            assert resolved.exists(), f"{path.name}: broken relative link -> {target}"


def test_kubernetes_doc_supported() -> None:
    """F24/AC20 — the Kubernetes guide documents a supported chart, not a preview."""
    text = (DOCS_DIR / "kubernetes.md").read_text(encoding="utf-8")
    # Locate the `## Status` section and assert it no longer says "preview".
    status = ""
    capture = False
    for line in text.splitlines():
        if line.startswith("## "):
            capture = line.strip().lower() == "## status"
            continue
        if capture:
            status += line + "\n"
    assert status.strip(), "kubernetes.md must have a `## Status` section"
    assert "preview" not in status.lower(), "kubernetes.md still marked as preview"
    assert "supported" in status.lower(), "kubernetes.md `## Status` must say supported"
    for ref in KUBERNETES_MUST_REFERENCE:
        assert ref in text, f"kubernetes.md must reference {ref!r}"
