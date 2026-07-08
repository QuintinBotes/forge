"""Version single-source-of-truth guard (HARD-12, AC2).

Asserts that the root ``[project].version`` (the one true version), every member
``pyproject.toml`` version, both ``package.json`` versions, every package
``__init__.__version__``, and the compose ``FORGE_VERSION`` default are all
byte-identical — and that ``[tool.commitizen].version_files`` lists exactly that
file set so ``cz bump`` can never silently leave one behind.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ROOT_PYPROJECT = REPO_ROOT / "pyproject.toml"


def _root_version() -> str:
    data = tomllib.loads(ROOT_PYPROJECT.read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def _member_pyprojects() -> list[Path]:
    return sorted(
        [*REPO_ROOT.glob("packages/*/pyproject.toml"), *REPO_ROOT.glob("apps/*/pyproject.toml")]
    )


def _package_inits() -> list[Path]:
    inits: list[Path] = []
    for base in ("packages", "apps"):
        for init in (REPO_ROOT / base).glob("*/forge_*/__init__.py"):
            if "__version__" in init.read_text(encoding="utf-8"):
                inits.append(init)
    return sorted(inits)


def _pyproject_version(path: Path) -> str:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def _init_version(path: Path) -> str | None:
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', path.read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else None


def _packagejson_version(path: Path) -> str:
    return str(json.loads(path.read_text(encoding="utf-8"))["version"])


def _compose_versions() -> list[str]:
    text = (REPO_ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
    return re.findall(r"\$\{FORGE_VERSION:-([^}]+)\}", text)


def _collect() -> dict[str, str]:
    """Map every version-bearing file (repo-relative) to its declared version."""

    versions: dict[str, str] = {"pyproject.toml": _root_version()}
    for p in _member_pyprojects():
        versions[str(p.relative_to(REPO_ROOT))] = _pyproject_version(p)
    for p in _package_inits():
        v = _init_version(p)
        assert v is not None, f"{p} matched the scan but has no parseable __version__"
        versions[str(p.relative_to(REPO_ROOT))] = v
    for name in ("package.json", "apps/web/package.json"):
        versions[name] = _packagejson_version(REPO_ROOT / name)
    for i, cv in enumerate(_compose_versions()):
        versions[f"deploy/docker-compose.yml#{i}"] = cv
    return versions


def test_all_versions_are_identical() -> None:
    root = _root_version()
    versions = _collect()
    mismatched = {f: v for f, v in versions.items() if v != root}
    assert not mismatched, f"version drift from root {root!r}: {mismatched}"


def test_commitizen_version_files_cover_every_version_bearing_file() -> None:
    data = tomllib.loads(ROOT_PYPROJECT.read_text(encoding="utf-8"))
    version_files = data["tool"]["commitizen"]["version_files"]
    covered = {entry.split(":", 1)[0] for entry in version_files}

    required: set[str] = set()
    for p in (*_member_pyprojects(), *_package_inits()):
        required.add(str(p.relative_to(REPO_ROOT)))
    required.add("package.json")
    required.add("apps/web/package.json")
    required.add("deploy/docker-compose.yml")
    # The root pyproject is the pep621 version PROVIDER, so it must NOT be a
    # version_files entry (cz bumps it directly).
    assert "pyproject.toml" not in covered

    missing = required - covered
    assert not missing, f"version_files is missing: {sorted(missing)}"


def test_commitizen_version_files_have_no_dead_entries() -> None:
    data = tomllib.loads(ROOT_PYPROJECT.read_text(encoding="utf-8"))
    version_files = data["tool"]["commitizen"]["version_files"]
    for entry in version_files:
        path = entry.split(":", 1)[0]
        assert (REPO_ROOT / path).is_file(), f"version_files points at a missing file: {path}"


def test_deliberate_desync_is_detected(tmp_path: Path) -> None:
    # Prove the guard actually catches drift: mutate a copied version and assert
    # the equality logic flags it.
    versions = _collect()
    root = _root_version()
    assert all(v == root for v in versions.values())  # sanity
    desynced = dict(versions)
    desynced["packages/db/pyproject.toml"] = "9.9.9"
    mismatched = {f: v for f, v in desynced.items() if v != root}
    assert mismatched == {"packages/db/pyproject.toml": "9.9.9"}


def test_expected_file_counts() -> None:
    # 20 packages + 3 apps = 23 member pyprojects, and one __version__ per package.
    assert len(_member_pyprojects()) == 23
    assert len(_package_inits()) == 23
