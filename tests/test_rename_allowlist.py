"""The project was renamed from "forge" to "Swarm Code Factory" (issue #248).

The old name may remain only where ``tests/rename_allowlist.toml`` says so, with a
reason. These tests fail when it appears in a tracked file anywhere else, and when
a name the rename script maps comes back.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import tomllib
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = tomllib.loads((ROOT / "tests" / "rename_allowlist.toml").read_text(encoding="utf-8"))
CATEGORIES = {"compatibility", "third-party", "historical"}
OLD = re.compile("forge", re.IGNORECASE)


@cache
def _tracked() -> dict[str, str]:
    """Text files by path: tracked, or new and not ignored. Binary files read as empty."""
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    files = {}
    for path in filter(None, out.split("\0")):
        try:
            files[path] = (ROOT / path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            files[path] = ""
    return files


def _allowed_path(path: str) -> bool:
    return any(path.startswith(entry["path"]) for entry in ALLOWLIST["paths"])


def _strip(path: str, text: str, used: set[int]) -> str:
    """``text`` with every allow-listed span removed."""
    for index, entry in enumerate(ALLOWLIST["spans"]):
        if "paths" in entry and not re.search(entry["paths"], path):
            continue
        text, count = re.subn(entry["pattern"], "\x00", text)
        if count:
            used.add(index)
    return text


def test_every_allowlist_entry_has_a_category_and_a_reason() -> None:
    for entry in [*ALLOWLIST["paths"], *ALLOWLIST["spans"]]:
        assert entry["category"] in CATEGORIES, entry
        assert entry["reason"].strip(), entry


def test_the_old_name_appears_only_where_the_allowlist_says() -> None:
    used: set[int] = set()
    found = []
    for path, text in _tracked().items():
        if _allowed_path(path):
            continue
        # The path is checked as a first line, so a file name is held to the same list.
        for number, line in enumerate(_strip(path, f"{path}\n{text}", used).split("\n")):
            if OLD.search(line):
                found.append(f"{path}:{number}: {line.strip()[:160]}")
    assert not found, (
        f'the old name "forge" appears in {len(found)} places that '
        "tests/rename_allowlist.toml does not list. Use the new name, or add an entry "
        "with a reason:\n" + "\n".join(found[:50])
    )
    unused = [e["pattern"] for i, e in enumerate(ALLOWLIST["spans"]) if i not in used]
    assert not unused, f"allow-list entries that match nothing; remove them: {unused}"
    dead = [
        e["path"]
        for e in ALLOWLIST["paths"]
        if not any(p.startswith(e["path"]) for p in _tracked())
    ]
    assert not dead, f"allow-list paths that no longer exist; remove them: {dead}"


def test_no_name_the_rename_maps_has_come_back() -> None:
    """The rename script finds nothing left to do: every mapped name is gone."""
    spec = importlib.util.spec_from_file_location(
        "rename_project", ROOT / "scripts/rename_project.py"
    )
    assert spec and spec.loader
    rename = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rename)
    assert rename.pending() == []
