"""Workspace substrate smoke tests (Task 0.1).

Every Python workspace member must be importable, expose a ``__version__``, and
ship a ``py.typed`` marker so downstream tasks can build against typed packages.
These tests are the green gate for the workspace + tooling skeleton.
"""

from __future__ import annotations

import importlib
from importlib import util as importlib_util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# (workspace directory, import package name) for every Python member.
PYTHON_MEMBERS: list[tuple[str, str]] = [
    ("packages/contracts", "forge_contracts"),
    ("packages/db", "forge_db"),
    ("packages/workflow-engine", "forge_workflow"),
    ("packages/agent-runtime", "forge_agent"),
    ("packages/multi-agent-coordinator", "forge_coordinator"),
    ("packages/spec-engine", "forge_spec"),
    ("packages/board-core", "forge_board"),
    ("packages/knowledge-core", "forge_knowledge"),
    ("packages/integration-sdk", "forge_integrations"),
    ("packages/mcp-sdk", "forge_mcp"),
    ("packages/policy-sdk", "forge_policy"),
    ("packages/skill-sdk", "forge_skill"),
    ("packages/evaluation", "forge_eval"),
    ("apps/api", "forge_api"),
    ("apps/worker", "forge_worker"),
    ("apps/mcp-gateway", "forge_mcp_gateway"),
]

IMPORT_NAMES = [pkg for _, pkg in PYTHON_MEMBERS]


@pytest.mark.parametrize("import_name", IMPORT_NAMES)
def test_member_is_importable_and_versioned(import_name: str) -> None:
    module = importlib.import_module(import_name)
    assert module.__version__ == "0.1.0"


@pytest.mark.parametrize(("member_dir", "import_name"), PYTHON_MEMBERS)
def test_member_has_pyproject_and_py_typed(member_dir: str, import_name: str) -> None:
    base = REPO_ROOT / member_dir
    assert (base / "pyproject.toml").is_file(), f"{member_dir} missing pyproject.toml"
    assert (base / import_name / "__init__.py").is_file(), f"{import_name} missing __init__.py"
    assert (base / import_name / "py.typed").is_file(), f"{import_name} missing py.typed"


@pytest.mark.parametrize("import_name", IMPORT_NAMES)
def test_member_ships_typed_marker_on_disk(import_name: str) -> None:
    spec = importlib_util.find_spec(import_name)
    assert spec is not None and spec.origin is not None
    assert (Path(spec.origin).parent / "py.typed").is_file()


def test_root_tooling_files_exist() -> None:
    for name in (
        "pyproject.toml",
        "ruff.toml",
        "mypy.ini",
        "Makefile",
        ".env.example",
        "LICENSE",
        "README.md",
        "pnpm-workspace.yaml",
    ):
        assert (REPO_ROOT / name).is_file(), f"missing root tooling file: {name}"
