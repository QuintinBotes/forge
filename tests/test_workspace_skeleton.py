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
    ("packages/contracts", "scf_contracts"),
    ("packages/db", "scf_db"),
    ("packages/workflow-engine", "scf_workflow"),
    ("packages/agent-runtime", "scf_agent"),
    ("packages/multi-agent-coordinator", "scf_coordinator"),
    ("packages/spec-engine", "scf_spec"),
    ("packages/board-core", "scf_board"),
    ("packages/knowledge-core", "scf_knowledge"),
    ("packages/integration-sdk", "scf_integrations"),
    ("packages/mcp-sdk", "scf_mcp"),
    ("packages/policy-sdk", "scf_policy"),
    ("packages/skill-sdk", "scf_skill"),
    ("packages/evaluation", "scf_eval"),
    ("apps/api", "scf_api"),
    ("apps/worker", "scf_worker"),
    ("apps/mcp-gateway", "scf_mcp_gateway"),
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


def _readme_layout_block() -> str:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Repository layout", 1)[1]
    return section.split("```", 2)[1]


@pytest.mark.parametrize("parent", ["apps", "packages"])
def test_readme_layout_lists_every_workspace_directory(parent: str) -> None:
    layout = _readme_layout_block()
    on_disk = sorted(
        p.name
        for p in (REPO_ROOT / parent).iterdir()
        if p.is_dir() and not p.name.startswith((".", "_"))
    )
    missing = [name for name in on_disk if f" {name}/" not in layout]
    assert not missing, f"README 'Repository layout' omits {parent}/: {missing}"
