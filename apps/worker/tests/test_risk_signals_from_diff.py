"""Change signals read from a git diff between a base and a head (#193 criterion 3)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from scf_worker.risk_classification import signals_from_diff


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo: Path, files: dict[str, bytes], message: str) -> str:
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    return repo, _commit(repo, {"README.md": b"one\ntwo\n"}, "init")


def test_signals_carry_paths_counts_and_what_the_paths_touch(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    head = _commit(
        repo,
        {
            "README.md": b"one\n2\n",
            "app/auth/login.py": b"a = 1\nb = 2\nc = 3\n",
            "db/migrations/0002_add.py": b"up\ndown\n",
            "api/v1.proto": b"syntax\n",
            "logo.png": b"\x89PNG\x00\x01\x02",
        },
        "change",
    )

    signals = signals_from_diff(repo, base, head)

    assert signals.available and signals.unavailable_reason is None
    assert sorted(signals.paths) == [
        "README.md",
        "api/v1.proto",
        "app/auth/login.py",
        "db/migrations/0002_add.py",
        "logo.png",
    ]
    assert signals.migration_present and signals.touches_contracts
    # README: 1 added, 1 removed; login: 3; migration: 2; proto: 1; a binary file counts none.
    assert (signals.lines_added, signals.lines_removed) == (7, 1)


def test_a_change_to_docs_alone_touches_no_migration_or_contract(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    head = _commit(repo, {"docs/guide.md": b"hello\n"}, "docs")

    signals = signals_from_diff(repo, base, head)

    assert signals.paths == ["docs/guide.md"]
    assert not signals.migration_present and not signals.touches_contracts
    assert (signals.lines_added, signals.lines_removed) == (1, 0)


def test_contract_and_schema_paths_are_read_from_directories_and_names(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    head = _commit(
        repo,
        {"packages/contracts/models.py": b"x\n", "alembic/versions/0001.py": b"y\n"},
        "contracts",
    )

    signals = signals_from_diff(repo, base, head)

    assert signals.touches_contracts and signals.migration_present


def test_an_unreadable_diff_returns_unavailable_signals_without_raising(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)

    missing_head = signals_from_diff(repo, base, "0" * 40)
    not_a_repo = signals_from_diff(tmp_path / "nowhere", base, base)
    written = tmp_path / "written"
    an_option = signals_from_diff(repo, base, f"--output={written}")

    for signals in (missing_head, not_a_repo, an_option):
        assert not signals.available
        assert signals.unavailable_reason
        assert signals.paths == []
    assert not written.exists()  # a ref is never read as an option
