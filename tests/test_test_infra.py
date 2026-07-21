"""Tests for the shared test infrastructure (plan Task 0.6).

Covers the root ``conftest`` helpers + Postgres fixtures and the registered
pytest markers. The live-Postgres path is exercised as a ``postgres``-marked
test that skips (parked) when no database is configured — the honest state in a
no-network sandbox.
"""

from __future__ import annotations

import os
from collections import defaultdict
from pathlib import Path

import conftest
import pytest


def test_resolve_test_database_url_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORGE_TEST_DATABASE_URL", raising=False)
    assert conftest.resolve_test_database_url() is None

    monkeypatch.setenv("FORGE_TEST_DATABASE_URL", "postgresql+psycopg://u:p@h:5432/db")
    assert conftest.resolve_test_database_url() == "postgresql+psycopg://u:p@h:5432/db"


def test_resolve_test_database_url_treats_empty_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FORGE_TEST_DATABASE_URL", "")
    assert conftest.resolve_test_database_url() is None


def test_postgres_and_integration_markers_registered(
    pytestconfig: pytest.Config,
) -> None:
    markers = "\n".join(pytestconfig.getini("markers"))
    assert "postgres" in markers
    assert "integration" in markers


@pytest.mark.postgres
def test_postgres_fixture_yields_url(postgres_url: str) -> None:
    # Runs only when Postgres is configured; otherwise skipped (parked).
    assert postgres_url


# --------------------------------------------------------------------------- #
# Guard: test-basename collisions (Task 9).
#
# Whole-repo pytest collection imports every ``test_*.py`` into one process.
# Python caches modules by their *dotted name*, not by file path, so two test
# files that share a basename AND derive the same dotted module name abort
# collection with ``exit 2`` ("import file mismatch") — silently redding the
# entire ``python`` CI lane. Today's duplicate basenames are each disambiguated
# by a chain of ``__init__.py`` package dirs (or the lack of one); this guard
# fails the moment a new copy is not, before it can red the lane.
#
# The guard stays strict (unique dotted-module per duplicated basename) even
# after adopting ``--import-mode=importlib`` — importlib tolerates such
# collisions, but keeping the tree collision-free keeps the suite portable and
# greppable regardless of import mode.
#
# See .superpowers/sdd/task-9-brief.md (Task 9).
# --------------------------------------------------------------------------- #

_EXCLUDED_DIRS = frozenset({"node_modules", ".git", "__pycache__", ".venv"})


def _repo_root() -> Path:
    """Anchor the walk at the repo root (nearest ancestor with pyproject.toml)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return here.parent.parent


def _dotted_module(path: Path) -> str:
    """Module name pytest's ``prepend`` import mode derives for *path*.

    Walk upward accumulating package names while an ``__init__.py`` is present;
    the first ancestor without one is the sys.path root. Two test files that
    derive the *same* dotted name collide at import time (``exit 2``).
    """
    parts = [path.stem]
    parent = path.parent
    while (parent / "__init__.py").exists():
        parts.append(parent.name)
        parent = parent.parent
    return ".".join(reversed(parts))


def _iter_test_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS]
        for filename in filenames:
            if filename.startswith("test_") and filename.endswith(".py"):
                yield Path(dirpath) / filename


def test_no_ambiguous_test_basename_collisions() -> None:
    """Duplicate test-file basenames must each resolve to a unique module.

    Fix a reported collision by either adding ``__init__.py`` package dirs so
    each copy sits under a *unique* dotted package path, or renaming the
    offending ``test_*.py`` to a unique basename. See Task 9 brief.
    """
    root = _repo_root()

    by_basename: dict[str, list[Path]] = defaultdict(list)
    for path in _iter_test_files(root):
        by_basename[path.name].append(path)

    collisions: list[str] = []
    for _basename, paths in sorted(by_basename.items()):
        if len(paths) < 2:
            continue
        by_module: dict[str, list[Path]] = defaultdict(list)
        for path in paths:
            by_module[_dotted_module(path)].append(path)
        for module, module_paths in sorted(by_module.items()):
            if len(module_paths) > 1:
                rels = ", ".join(
                    str(p.relative_to(root)) for p in sorted(module_paths)
                )
                collisions.append(f"  module {module!r} <- {rels}")

    assert not collisions, (
        "Ambiguous test-basename collision(s): the following test files share a "
        "basename AND resolve to the SAME dotted module, which aborts whole-repo "
        "pytest collection with exit 2 (import file mismatch):\n"
        + "\n".join(collisions)
        + "\n\nFix: add __init__.py package dirs to give each copy a unique dotted "
        "module path, or rename one file to a unique basename. "
        "See .superpowers/sdd/task-9-brief.md (Task 9)."
    )
