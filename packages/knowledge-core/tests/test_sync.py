"""Tests for ``forge_knowledge.sync`` (plan Task 1.4, RAG spine).

Task 1.4 adds the two *sync modes* on top of the Task 1.2 stores:

* **full sync** — (re)chunk and index every file of a source, idempotent by
  content hash, pruning chunks whose file no longer exists;
* **incremental sync** — given a git diff, (re)index *only* the changed files,
  delete the chunks of removed/renamed-away files, and never touch the rest.

The headline behaviours proved here:
- a full sync indexes one-or-more chunks per file and is idempotent on re-run;
- an incremental re-index touches **only** the changed files (every other file's
  chunk rows are byte-for-byte the same row ids afterwards);
- ``git_changed_files`` parses a *real* ``git diff --name-status`` against a
  throwaway repository (added / modified / deleted / renamed), with no network.

Hermetic: in-memory SQLite, a local throwaway git repo in ``tmp_path``. No
network, no live services.
"""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from forge_contracts.dtos import Chunk, KnowledgeScope
from forge_contracts.enums import ChunkType, SyncMode
from forge_db.base import Base
from forge_db.models import KnowledgeSource, RetrievalChunk, Workspace
from forge_db.session import create_session_factory
from forge_knowledge import sync as sync_module
from forge_knowledge.embeddings import DeterministicEmbeddingClient
from forge_knowledge.stores import PgVectorStore
from forge_knowledge.sync import (
    DEFAULT_MAX_COMMITS,
    MAX_COMMITS,
    ChangeSet,
    commit_limit,
    full_sync,
    git_changed_files,
    git_log,
    incremental_sync,
    iter_source_files,
    read_repo_files,
    sync_source,
    sync_tracker,
)

# A small synthetic repo: each value is distinct so re-chunking is observable.
CORPUS: dict[str, str] = {
    "db/pool.py": "def connect_postgres():\n    return open_pool('psycopg')\n",
    "auth/jwt.py": "def validate_jwt(token):\n    return verify(token)\n",
    "search/rrf.py": "def compute_rrf_score(rankings):\n    return fuse(rankings)\n",
    "README.md": "# App\n\nThe application service.\n",
}


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


@pytest.fixture
def workspace_id(session_factory: sessionmaker[Session]) -> uuid.UUID:
    with session_factory() as session:
        workspace = Workspace(name="Acme", slug="acme")
        session.add(workspace)
        session.flush()
        ws_id = workspace.id
        session.commit()
    return ws_id


@pytest.fixture
def source_id(session_factory: sessionmaker[Session], workspace_id: uuid.UUID) -> uuid.UUID:
    with session_factory() as session:
        source = KnowledgeSource(
            workspace_id=workspace_id, kind="repo", name="app", uri="github.com/org/app"
        )
        session.add(source)
        session.flush()
        src_id = source.id
        session.commit()
    return src_id


@pytest.fixture
def store(session_factory: sessionmaker[Session]) -> PgVectorStore:
    return PgVectorStore(session_factory, DeterministicEmbeddingClient(dimension=128))


def _rows_by_path(
    session_factory: sessionmaker[Session], source_id: uuid.UUID
) -> dict[str, list[tuple[uuid.UUID, str]]]:
    """Map ``path -> [(row_id, content_hash), ...]`` for a source."""
    out: dict[str, list[tuple[uuid.UUID, str]]] = {}
    with session_factory() as session:
        rows = session.scalars(
            select(RetrievalChunk).where(RetrievalChunk.knowledge_source_id == source_id)
        )
        for row in rows:
            out.setdefault(row.path or "", []).append((row.id, row.content_hash))
    return out


# --------------------------------------------------------------------------- #
# Store deletion / introspection helpers (foundation for incremental sync)     #
# --------------------------------------------------------------------------- #


def test_store_source_paths_lists_indexed_paths(store: PgVectorStore, source_id: uuid.UUID) -> None:
    full_sync(store, str(source_id), CORPUS)
    assert store.source_paths(str(source_id)) == set(CORPUS)


def test_store_delete_source_paths_removes_only_those_paths(
    store: PgVectorStore,
    source_id: uuid.UUID,
    session_factory: sessionmaker[Session],
) -> None:
    full_sync(store, str(source_id), CORPUS)
    deleted = store.delete_source_paths(str(source_id), ["auth/jwt.py"])
    assert deleted >= 1
    remaining = set(_rows_by_path(session_factory, source_id))
    assert "auth/jwt.py" not in remaining
    assert "db/pool.py" in remaining


# --------------------------------------------------------------------------- #
# Full sync                                                                    #
# --------------------------------------------------------------------------- #


def test_full_sync_indexes_all_files(
    store: PgVectorStore,
    source_id: uuid.UUID,
    session_factory: sessionmaker[Session],
) -> None:
    result = full_sync(store, str(source_id), CORPUS)
    assert result.indexed >= len(CORPUS)
    assert set(_rows_by_path(session_factory, source_id)) == set(CORPUS)


def test_full_sync_is_idempotent(store: PgVectorStore, source_id: uuid.UUID) -> None:
    first = full_sync(store, str(source_id), CORPUS)
    second = full_sync(store, str(source_id), CORPUS)
    assert second.indexed == 0
    assert second.skipped == first.indexed
    assert second.deleted == 0


def test_full_sync_prunes_files_that_disappeared(
    store: PgVectorStore,
    source_id: uuid.UUID,
    session_factory: sessionmaker[Session],
) -> None:
    full_sync(store, str(source_id), CORPUS)
    smaller = {k: v for k, v in CORPUS.items() if k != "auth/jwt.py"}

    result = full_sync(store, str(source_id), smaller)

    assert result.deleted >= 1
    assert "auth/jwt.py" not in _rows_by_path(session_factory, source_id)


def test_full_sync_without_prune_keeps_orphans(
    store: PgVectorStore,
    source_id: uuid.UUID,
    session_factory: sessionmaker[Session],
) -> None:
    full_sync(store, str(source_id), CORPUS)
    smaller = {k: v for k, v in CORPUS.items() if k != "auth/jwt.py"}

    result = full_sync(store, str(source_id), smaller, prune=False)

    assert result.deleted == 0
    assert "auth/jwt.py" in _rows_by_path(session_factory, source_id)


class _WatchedStore(PgVectorStore):
    """Records the source's chunk contents at the moment of every deletion."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        super().__init__(session_factory, None)
        self._factory = session_factory
        self.seen_at_delete: list[set[str]] = []

    def _contents(self) -> set[str]:
        with self._factory() as session:
            return set(session.scalars(select(RetrievalChunk.content)))

    def delete_stale(self, source_id, current):  # type: ignore[no-untyped-def]
        self.seen_at_delete.append(self._contents())
        return super().delete_stale(source_id, current)

    def delete_source_paths(self, source_id, paths):  # type: ignore[no-untyped-def]
        self.seen_at_delete.append(self._contents())
        return super().delete_source_paths(source_id, paths)


def test_resync_after_chunker_change_replaces_old_chunks_without_a_gap(
    session_factory: sessionmaker[Session], source_id: uuid.UUID
) -> None:
    # The index as the pre-D-a paragraph chunker left it: no breadcrumb lines.
    store = _WatchedStore(session_factory)
    docs = {"docs/guide.md": "# Guide\n\nFirst paragraph.\n\nSecond paragraph.\n"}
    old = [
        Chunk(content=text, chunk_type=ChunkType.MARKDOWN, path="docs/guide.md")
        for text in ("# Guide", "First paragraph.", "Second paragraph.")
    ]
    store.index(str(source_id), old)

    result = full_sync(store, str(source_id), docs)

    new_content = "docs/guide.md › Guide\n# Guide\n\nFirst paragraph.\n\nSecond paragraph."  # noqa: RUF001
    assert store._contents() == {new_content}
    assert (result.indexed, result.deleted) == (1, 3)
    # Every deletion happened with the new chunk already written.
    assert store.seen_at_delete
    assert all(new_content in seen for seen in store.seen_at_delete)
    # The next sync writes and deletes nothing.
    again = full_sync(store, str(source_id), docs)
    assert (again.indexed, again.deleted) == (0, 0)


def test_full_sync_replaces_a_changed_files_stale_chunks(
    store: PgVectorStore, source_id: uuid.UUID, session_factory: sessionmaker[Session]
) -> None:
    full_sync(store, str(source_id), CORPUS)
    changed = {**CORPUS, "auth/jwt.py": "def validate_jwt(token):\n    return verify_v2(token)\n"}
    full_sync(store, str(source_id), changed)
    with session_factory() as session:
        jwt = list(
            session.scalars(
                select(RetrievalChunk.content).where(RetrievalChunk.path == "auth/jwt.py")
            )
        )
    assert all("verify(token)" not in text for text in jwt)
    assert any("verify_v2(token)" in text for text in jwt)


# --------------------------------------------------------------------------- #
# Incremental sync — the headline "touches only changed files" property        #
# --------------------------------------------------------------------------- #


def test_incremental_sync_touches_only_modified_file(
    store: PgVectorStore,
    source_id: uuid.UUID,
    session_factory: sessionmaker[Session],
) -> None:
    full_sync(store, str(source_id), CORPUS)
    before = _rows_by_path(session_factory, source_id)

    updated = dict(CORPUS)
    updated["auth/jwt.py"] = "def validate_jwt(token):\n    return verify_v2(token)\n"
    changes = ChangeSet(modified=["auth/jwt.py"])

    result = incremental_sync(store, str(source_id), updated, changes)
    after = _rows_by_path(session_factory, source_id)

    # The modified file was re-chunked: its rows changed identity / hash.
    assert after["auth/jwt.py"] != before["auth/jwt.py"]
    assert result.indexed >= 1
    assert result.updated >= 1
    # Every other file is byte-for-byte the same rows (untouched).
    for path in CORPUS:
        if path != "auth/jwt.py":
            assert after[path] == before[path], f"{path} was unexpectedly touched"


def test_incremental_sync_adds_new_file(
    store: PgVectorStore,
    source_id: uuid.UUID,
    session_factory: sessionmaker[Session],
) -> None:
    full_sync(store, str(source_id), CORPUS)
    before = _rows_by_path(session_factory, source_id)

    files = dict(CORPUS)
    files["infra/k8s.py"] = "def schedule_pod():\n    return scheduler.run()\n"
    changes = ChangeSet(added=["infra/k8s.py"])

    result = incremental_sync(store, str(source_id), files, changes)
    after = _rows_by_path(session_factory, source_id)

    assert "infra/k8s.py" in after
    assert result.indexed >= 1
    for path in CORPUS:
        assert after[path] == before[path]


def test_incremental_sync_deletes_removed_file(
    store: PgVectorStore,
    source_id: uuid.UUID,
    session_factory: sessionmaker[Session],
) -> None:
    full_sync(store, str(source_id), CORPUS)
    changes = ChangeSet(deleted=["search/rrf.py"])

    result = incremental_sync(store, str(source_id), CORPUS, changes)
    after = _rows_by_path(session_factory, source_id)

    assert "search/rrf.py" not in after
    assert result.deleted >= 1


def test_incremental_sync_handles_rename(
    store: PgVectorStore,
    source_id: uuid.UUID,
    session_factory: sessionmaker[Session],
) -> None:
    full_sync(store, str(source_id), CORPUS)

    files = {k: v for k, v in CORPUS.items() if k != "search/rrf.py"}
    files["search/fusion.py"] = CORPUS["search/rrf.py"]
    changes = ChangeSet(renamed=[("search/rrf.py", "search/fusion.py")])

    result = incremental_sync(store, str(source_id), files, changes)
    after = _rows_by_path(session_factory, source_id)

    assert "search/rrf.py" not in after
    assert "search/fusion.py" in after
    assert result.deleted >= 1
    assert result.indexed >= 1


def test_incremental_sync_no_changes_is_noop(store: PgVectorStore, source_id: uuid.UUID) -> None:
    full_sync(store, str(source_id), CORPUS)
    result = incremental_sync(store, str(source_id), CORPUS, ChangeSet())
    assert result.indexed == 0
    assert result.deleted == 0
    assert result.updated == 0


def test_incremental_result_still_searchable(store: PgVectorStore, source_id: uuid.UUID) -> None:
    full_sync(store, str(source_id), CORPUS)
    updated = dict(CORPUS)
    updated["auth/jwt.py"] = "def validate_jwt(token):\n    return verify_audience(token)\n"
    incremental_sync(store, str(source_id), updated, ChangeSet(modified=["auth/jwt.py"]))

    hits = store.search("verify_audience", KnowledgeScope(), k=3)
    assert any(h.path == "auth/jwt.py" for h in hits)


# --------------------------------------------------------------------------- #
# ChangeSet helpers                                                            #
# --------------------------------------------------------------------------- #


def test_changeset_partitions_paths() -> None:
    changes = ChangeSet(
        added=["a.py"],
        modified=["b.py"],
        deleted=["c.py"],
        renamed=[("old.py", "new.py")],
    )
    assert changes.changed_paths == {"a.py", "b.py", "new.py"}
    assert changes.removed_paths == {"c.py", "old.py"}
    assert not changes.is_empty
    assert ChangeSet().is_empty


# --------------------------------------------------------------------------- #
# Filesystem walking                                                           #
# --------------------------------------------------------------------------- #


def test_iter_source_files_skips_excluded(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / "docs.md").write_text("# Docs\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "x.pyc").write_bytes(b"\x00\x01")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n")

    found = dict(iter_source_files(tmp_path))

    assert set(found) == {"src/app.py", "docs.md"}
    assert found["src/app.py"] == "print('hi')\n"


def test_read_repo_files_round_trips(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("a = 1\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("# b\n", encoding="utf-8")
    assert read_repo_files(tmp_path) == {"a.py": "a = 1\n", "b.md": "# b\n"}


# --------------------------------------------------------------------------- #
# git_changed_files — against a real throwaway repository                      #
# --------------------------------------------------------------------------- #


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Iterator[Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "forge@example.com")
    _git(repo, "config", "user.name", "Forge Test")
    _git(repo, "config", "commit.gpgsign", "false")
    yield repo


def test_git_changed_files_between_two_commits(git_repo: Path) -> None:
    (git_repo / "keep.py").write_text("def keep(): pass\n", encoding="utf-8")
    (git_repo / "drop.py").write_text("def drop(): pass\n", encoding="utf-8")
    (git_repo / "edit.py").write_text("def edit(): return 1\n", encoding="utf-8")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-q", "-m", "baseline")

    (git_repo / "edit.py").write_text("def edit(): return 2\n", encoding="utf-8")
    (git_repo / "drop.py").unlink()
    (git_repo / "new.py").write_text("def new(): pass\n", encoding="utf-8")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-q", "-m", "change")

    changes = git_changed_files(git_repo, "HEAD~1", "HEAD")

    assert "new.py" in changes.added
    assert "edit.py" in changes.modified
    assert "drop.py" in changes.deleted
    assert "keep.py" not in changes.changed_paths


def test_git_changed_files_detects_rename(git_repo: Path) -> None:
    body = "def stable_function_body():\n    return 'x' * 40\n"
    (git_repo / "old_name.py").write_text(body, encoding="utf-8")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-q", "-m", "baseline")

    _git(git_repo, "mv", "old_name.py", "new_name.py")
    _git(git_repo, "commit", "-q", "-m", "rename")

    changes = git_changed_files(git_repo, "HEAD~1", "HEAD")

    assert ("old_name.py", "new_name.py") in changes.renamed
    assert changes.removed_paths == {"old_name.py"}
    assert "new_name.py" in changes.changed_paths


def test_git_changed_files_against_working_tree(git_repo: Path) -> None:
    (git_repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-q", "-m", "baseline")

    (git_repo / "a.py").write_text("a = 2\n", encoding="utf-8")

    changes = git_changed_files(git_repo, "HEAD")
    assert "a.py" in changes.modified


# --------------------------------------------------------------------------- #
# sync_source — filesystem + git glue                                          #
# --------------------------------------------------------------------------- #


def test_sync_source_full_reads_filesystem(
    store: PgVectorStore,
    source_id: uuid.UUID,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Title\n\nbody\n", encoding="utf-8")

    result = sync_source(store, str(source_id), tmp_path, mode=SyncMode.FULL)

    assert result.indexed >= 2
    assert set(_rows_by_path(session_factory, source_id)) == {"pkg/mod.py", "README.md"}


def test_sync_source_incremental_reindexes_only_changed(
    store: PgVectorStore,
    source_id: uuid.UUID,
    session_factory: sessionmaker[Session],
    git_repo: Path,
) -> None:
    (git_repo / "stable.py").write_text("def stable(): return 1\n", encoding="utf-8")
    (git_repo / "churn.py").write_text("def churn(): return 1\n", encoding="utf-8")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-q", "-m", "baseline")

    sync_source(store, str(source_id), git_repo, mode=SyncMode.FULL)
    before = _rows_by_path(session_factory, source_id)

    (git_repo / "churn.py").write_text("def churn(): return 99\n", encoding="utf-8")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-q", "-m", "churn")

    result = sync_source(
        store, str(source_id), git_repo, mode=SyncMode.INCREMENTAL, base_ref="HEAD~1"
    )
    after = _rows_by_path(session_factory, source_id)

    assert result.indexed >= 1
    assert after["stable.py"] == before["stable.py"]
    assert after["churn.py"] != before["churn.py"]


def test_sync_source_incremental_requires_base_ref(
    store: PgVectorStore, source_id: uuid.UUID, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="base_ref"):
        sync_source(store, str(source_id), tmp_path, mode=SyncMode.INCREMENTAL)


# --------------------------------------------------------------------------- #
# Commit history (spec J §2.3)                                                 #
# --------------------------------------------------------------------------- #


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_git_log_reads_commits_with_their_paths(git_repo: Path) -> None:
    (git_repo / "docs").mkdir()
    (git_repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    (git_repo / "docs" / "caf\u00e9.md").write_text("# Caf\u00e9\n", encoding="utf-8")
    _git(git_repo, "add", "-A")
    message = ["-m", "Add a and docs (#3)", "-m", "Why: a test.\n\nSecond para."]
    _git(git_repo, "commit", "-q", *message)
    first = _head(git_repo)
    (git_repo / "a.py").write_text("a = 2\n", encoding="utf-8")
    _git(git_repo, "commit", "-q", "-am", "Bump a")
    second = _head(git_repo)

    records = list(git_log(git_repo, "HEAD"))

    assert [r.sha for r in records] == [second, first]
    assert [r.subject for r in records] == ["Bump a", "Add a and docs (#3)"]
    assert records[1].body == "Why: a test.\n\nSecond para."
    assert records[0].body == ""
    assert records[1].paths == ("a.py", "docs/caf\u00e9.md")
    assert records[0].paths == ("a.py",)
    assert records[0].committed_at.tzinfo is not None
    assert list(git_log(git_repo, f"{first}..HEAD")) == records[:1]
    with pytest.raises(ValueError, match="revision"):
        list(git_log(git_repo, "--output=/tmp/x"))


def test_repo_sync_indexes_commit_history_idempotently(
    store: PgVectorStore,
    source_id: uuid.UUID,
    session_factory: sessionmaker[Session],
    git_repo: Path,
) -> None:
    (git_repo / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-q", "-m", "Add a")
    first = _head(git_repo)
    on = {"max_commits": DEFAULT_MAX_COMMITS}

    sync_source(store, str(source_id), git_repo, mode=SyncMode.FULL, **on)
    rows = _rows_by_path(session_factory, source_id)
    assert f"commits/{first}" in rows
    again = sync_source(store, str(source_id), git_repo, mode=SyncMode.FULL, **on)
    assert (again.indexed, again.deleted) == (0, 0)

    (git_repo / "a.py").write_text("def a():\n    return 2\n", encoding="utf-8")
    _git(git_repo, "commit", "-q", "-am", "Change a")
    second = _head(git_repo)
    sync_source(store, str(source_id), git_repo, mode=SyncMode.INCREMENTAL, base_ref="HEAD~1", **on)
    rows = _rows_by_path(session_factory, source_id)
    assert {f"commits/{first}", f"commits/{second}"} <= set(rows)
    with session_factory() as session:
        content = session.scalars(
            select(RetrievalChunk.content).where(RetrievalChunk.path == f"commits/{second}")
        ).one()
    assert content.startswith(f"{second} ") and content.endswith("Change a\na.py")

    final = sync_source(store, str(source_id), git_repo, mode=SyncMode.FULL, **on)
    assert (final.indexed, final.deleted) == (0, 0)


def test_repo_sync_of_a_repository_without_commits_indexes_files_only(
    store: PgVectorStore,
    source_id: uuid.UUID,
    session_factory: sessionmaker[Session],
    git_repo: Path,
) -> None:
    (git_repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    sync_source(store, str(source_id), git_repo, mode=SyncMode.FULL, max_commits=5)
    assert set(_rows_by_path(session_factory, source_id)) == {"a.py"}


def _commits(repo: Path, count: int) -> list[str]:
    """Make ``count`` commits touching ``a.py``; their shas, oldest first."""
    shas = []
    for i in range(count):
        (repo / "a.py").write_text(f"a = {i}\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", f"Commit {i}")
        shas.append(_head(repo))
    return shas


def _commit_paths(session_factory: sessionmaker[Session], source_id: uuid.UUID) -> set[str]:
    return {p for p in _rows_by_path(session_factory, source_id) if p.startswith("commits/")}


def test_commit_history_is_opt_in_and_bounded(
    store: PgVectorStore,
    source_id: uuid.UUID,
    session_factory: sessionmaker[Session],
    git_repo: Path,
) -> None:
    shas = _commits(git_repo, 3)

    # An existing source (no config.commits) gets no commit chunks.
    sync_source(store, str(source_id), git_repo, mode=SyncMode.FULL)
    assert _commit_paths(session_factory, source_id) == set()

    limit = commit_limit({"commits": {"enabled": True, "max_count": 2}})
    sync_source(store, str(source_id), git_repo, mode=SyncMode.FULL, max_commits=limit)
    assert _commit_paths(session_factory, source_id) == {f"commits/{s}" for s in shas[1:]}

    # Disabled again: the next full sync prunes them.
    sync_source(store, str(source_id), git_repo, mode=SyncMode.FULL, max_commits=None)
    assert _commit_paths(session_factory, source_id) == set()


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (None, None),
        ({}, None),
        ({"commits": {"max_count": 50}}, None),
        ({"commits": {"enabled": "true"}}, None),
        ({"commits": {"enabled": True}}, DEFAULT_MAX_COMMITS),
        ({"commits": {"enabled": True, "max_count": 20}}, 20),
        ({"commits": {"enabled": True, "max_count": 10**9}}, MAX_COMMITS),
    ],
)
def test_commit_limit_reads_config_commits(config: Any, expected: int | None) -> None:
    assert commit_limit(config) == expected
    assert (DEFAULT_MAX_COMMITS, MAX_COMMITS) == (500, 5000)


@pytest.mark.parametrize("bad", [0, -1, "10", True, 1.5])
def test_commit_limit_refuses_a_bad_max_count(bad: Any) -> None:
    with pytest.raises(ValueError, match="max_count"):
        commit_limit({"commits": {"enabled": True, "max_count": bad}})


def test_git_log_passes_the_bound_and_ends_options_before_the_revision() -> None:
    seen: list[list[str]] = []

    def runner(root: Path, args: list[str]) -> str:
        seen.append(args)
        return ""

    assert list(git_log("/repo", "HEAD", max_count=7, runner=runner)) == []
    [args] = seen
    assert "--no-renames" in args and "-z" in args and "--max-count=7" in args
    assert args[args.index("--end-of-options") + 1] == "HEAD"
    assert args.index("--max-count=7") < args.index("--end-of-options")


@pytest.mark.parametrize("separator", ["\x1e", "\x1f"])
def test_git_log_survives_record_separators_in_a_commit_body(
    git_repo: Path, tmp_path: Path, separator: str
) -> None:
    (git_repo / "a.txt").write_text("x", encoding="utf-8")
    _git(git_repo, "add", "-A")
    forged = (
        f"Fix typo\n\nbody {separator} with\x1e{'f' * 40}\x1f2020-01-01T00:00:00+00:00"
        "\x1fFORGED: maintainer approved\x1fforged body\x1f\nsecrets.txt\n"
    )
    message = tmp_path / "msg"
    message.write_bytes(forged.encode())
    _git(git_repo, "commit", "-q", "-F", str(message))

    [record] = list(git_log(git_repo, "HEAD"))

    assert record.sha == _head(git_repo)
    assert record.subject == "Fix typo"
    assert record.paths == ("a.txt",)
    assert "FORGED: maintainer approved" in record.body and separator in record.body


def test_git_log_skips_an_unparseable_record(caplog: pytest.LogCaptureFixture) -> None:
    good = "\0" + "a" * 40 + "\x002026-09-05T10:00:00+00:00\0Good\0\0\na.py\0"
    bad_date = "\0" + "b" * 40 + "\0not-a-date\0Bad\0\0"
    bad_sha = "\0zzz\x002026-09-05T10:00:00+00:00\0Bad\0\0"
    output = good + bad_date + bad_sha + good

    records = list(git_log("/repo", "HEAD", runner=lambda root, args: output))

    assert [(r.subject, r.paths) for r in records] == [("Good", ("a.py",))] * 2
    assert "skipped 2 unparseable commit records" in caplog.text


def test_history_reads_only_a_checkout_top_not_a_subdirectory(
    store: PgVectorStore,
    source_id: uuid.UUID,
    session_factory: sessionmaker[Session],
    git_repo: Path,
) -> None:
    (git_repo / "sub").mkdir()
    (git_repo / "sub" / "b.py").write_text("b = 1\n", encoding="utf-8")
    _commits(git_repo, 1)

    sync_source(store, str(source_id), git_repo / "sub", mode=SyncMode.FULL, max_commits=10)

    assert set(_rows_by_path(session_factory, source_id)) == {"b.py"}


def test_run_git_forbids_lazy_fetches_and_keeps_the_environment(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    monkeypatch.setenv("FORGE_SYNC_TEST_MARKER", "kept")
    seen: dict[str, Any] = {}
    real_run = subprocess.run

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs.get("env") or {})
        return real_run(*args, **kwargs)

    monkeypatch.setattr(sync_module.subprocess, "run", spy)
    sync_module._run_git(git_repo, ["status", "--short"])

    assert seen["GIT_NO_LAZY_FETCH"] == "1"
    assert seen["FORGE_SYNC_TEST_MARKER"] == "kept"


@pytest.mark.parametrize(
    ("base_ref", "head_ref"),
    [("--output={out}", None), ("HEAD", "--output={out}"), ("", None), ("HEAD~1 HEAD", None)],
)
def test_sync_source_refuses_a_ref_git_could_read_as_an_option(
    store: PgVectorStore,
    source_id: uuid.UUID,
    git_repo: Path,
    tmp_path: Path,
    base_ref: str,
    head_ref: str | None,
) -> None:
    _commits(git_repo, 2)
    out = tmp_path / "written_by_git.txt"
    refs = {"base_ref": base_ref.format(out=out)}
    if head_ref is not None:
        refs["head_ref"] = head_ref.format(out=out)

    with pytest.raises(ValueError, match="revision"):
        sync_source(store, str(source_id), git_repo, mode=SyncMode.INCREMENTAL, **refs)
    with pytest.raises(ValueError, match="revision"):
        git_changed_files(git_repo, **refs)
    assert not out.exists()


def test_git_changed_files_ends_options_before_the_revision() -> None:
    seen: list[list[str]] = []
    git_changed_files("/repo", "main", "topic", runner=lambda root, args: seen.append(args) or "")
    assert seen == [["diff", "--name-status", "-M", "--end-of-options", "main..topic"]]


class _FlakyEmbedder(DeterministicEmbeddingClient):
    fail = False

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.fail:
            raise RuntimeError("provider down")
        return super().embed(texts)


def test_a_failed_incremental_write_leaves_the_old_chunks(
    source_id: uuid.UUID, session_factory: sessionmaker[Session]
) -> None:
    embedder = _FlakyEmbedder(dimension=128)
    store = PgVectorStore(session_factory, embedder)
    full_sync(store, str(source_id), CORPUS)
    before = _rows_by_path(session_factory, source_id)

    embedder.fail = True
    edited = {**CORPUS, "auth/jwt.py": "def validate_jwt(token):\n    return verify_v3(token)\n"}
    changes = ChangeSet(modified=["auth/jwt.py"], deleted=["search/rrf.py"])
    with pytest.raises(RuntimeError, match="provider down"):
        incremental_sync(store, str(source_id), edited, changes)

    assert _rows_by_path(session_factory, source_id) == before


def test_a_renamed_file_keeps_its_chunks_through_an_embedder_outage(
    source_id: uuid.UUID, session_factory: sessionmaker[Session]
) -> None:
    """A pure rename re-writes deduplicated chunks after the old rows go: that
    write must not call the provider, or an outage leaves the file with none."""
    embedder = _FlakyEmbedder(dimension=128)
    store = PgVectorStore(session_factory, embedder)
    body = "export const LIMIT = 10;\nexport const NAME = 'forge';\n"  # no symbol summary
    full_sync(store, str(source_id), {"web/a.ts": body})

    embedder.fail = True
    full_sync(store, str(source_id), {"web/b.ts": body})

    rows = _rows_by_path(session_factory, source_id)
    assert set(rows) == {"web/b.ts"}
    assert rows["web/b.ts"]


def test_full_sync_moves_a_function_to_its_new_file(
    store: PgVectorStore, source_id: uuid.UUID, session_factory: sessionmaker[Session]
) -> None:
    moved = "def moved(x):\n    return x * 2\n"
    other = "def other():\n    return 1\n"
    full_sync(store, str(source_id), {"a.py": moved, "b.py": other})

    full_sync(store, str(source_id), {"a.py": other, "b.py": moved})

    with session_factory() as session:
        paths = {
            (row.path, row.chunk_type.value)
            for row in session.scalars(
                select(RetrievalChunk).where(RetrievalChunk.content.contains("moved"))
            )
        }
    assert paths == {("b.py", "code"), ("b.py", "summary")}
    again = full_sync(store, str(source_id), {"a.py": other, "b.py": moved})
    assert (again.indexed, again.deleted) == (0, 0)


# --------------------------------------------------------------------------- #
# Tracker source (spec J §2.3)                                                 #
# --------------------------------------------------------------------------- #

_API = "https://api.github.com/repos/acme/widgets"


class _FakeGitHub:
    """``iter_pages`` over hand-written listings in GitHub's REST shapes."""

    def __init__(self) -> None:
        self.pages: dict[str, list[dict[str, Any]]] = {
            "issues": [
                {
                    "number": 7,
                    "title": "Search drops hits",
                    "state": "open",
                    "user": {"login": "ana"},
                    "labels": [{"name": "bug"}],
                    "body": "Steps to reproduce.",
                    "created_at": "2026-09-01T10:00:00Z",
                    "closed_at": None,
                },
                # The issues listing returns PRs too; they come from /pulls instead.
                {"number": 9, "title": "Fix", "pull_request": {"url": f"{_API}/pulls/9"}},
            ],
            "pulls": [
                {
                    "number": 9,
                    "title": "Fix search",
                    "state": "closed",
                    "user": {"login": "bo"},
                    "labels": [],
                    "body": "Fixes #7.",
                    "created_at": "2026-09-02T10:00:00Z",
                    "closed_at": "2026-09-03T10:00:00Z",
                    "merged_at": "2026-09-03T10:00:00Z",
                    "merge_commit_sha": "abc123",
                }
            ],
            "issues/comments": [
                {
                    "user": {"login": "cy"},
                    "body": "Seen on main.",
                    "created_at": "2026-09-01T12:00:00Z",
                    "issue_url": f"{_API}/issues/7",
                },
                {
                    "user": {"login": "ana"},
                    "body": "Thanks!",
                    "created_at": "2026-09-03T11:00:00Z",
                    "issue_url": f"{_API}/issues/9",
                },
            ],
            "pulls/comments": [
                {
                    "user": {"login": "cy"},
                    "body": "Rename this.",
                    "path": "search.py",
                    "created_at": "2026-09-02T12:00:00Z",
                    "pull_request_url": f"{_API}/pulls/9",
                }
            ],
        }
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def iter_pages(
        self, repo: str, path: str, params: Mapping[str, str] | None = None
    ) -> Iterator[dict[str, Any]]:
        self.calls.append((repo, path, dict(params or {})))
        return iter(self.pages[path])


@pytest.fixture
def tracker_source(
    session_factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> KnowledgeSource:
    with session_factory() as session:
        source = KnowledgeSource(
            workspace_id=workspace_id,
            kind="url",
            name="widgets tracker",
            uri="https://github.com/acme/widgets",
            config={"tracker": "github"},
        )
        session.add(source)
        session.commit()
    return source


def test_tracker_sync_indexes_issues_and_pulls_and_is_idempotent(
    store: PgVectorStore,
    session_factory: sessionmaker[Session],
    tracker_source: KnowledgeSource,
) -> None:
    github = _FakeGitHub()

    first = sync_tracker(store, tracker_source, github)

    rows = _rows_by_path(session_factory, tracker_source.id)
    assert set(rows) == {"issues/7", "pulls/9"}
    assert first.indexed == 2
    with session_factory() as session:
        pull = session.scalars(
            select(RetrievalChunk.content).where(RetrievalChunk.path == "pulls/9")
        ).one()
    assert pull.startswith("PR #9 [merged 2026-09-03, merge commit abc123] Fix search\n")
    assert pull.index("cy (2026-09-02) on search.py:") < pull.index("ana (2026-09-03):")
    assert {(repo, path) for repo, path, _ in github.calls} == {
        ("acme/widgets", p) for p in github.pages
    }
    assert all(params["per_page"] == "100" for _, _, params in github.calls)
    assert {p: q.get("state") for _, p, q in github.calls}["issues"] == "all"

    second = sync_tracker(store, tracker_source, github)
    assert (second.indexed, second.deleted) == (0, 0)
    assert _rows_by_path(session_factory, tracker_source.id) == rows


def test_tracker_sync_replaces_an_edited_item_and_prunes_a_vanished_one(
    store: PgVectorStore,
    session_factory: sessionmaker[Session],
    tracker_source: KnowledgeSource,
) -> None:
    github = _FakeGitHub()
    sync_tracker(store, tracker_source, github)
    before = _rows_by_path(session_factory, tracker_source.id)

    github.pages["issues/comments"].append(
        {
            "user": {"login": "bo"},
            "body": "Closing via #9.",
            "created_at": "2026-09-04T09:00:00Z",
            "issue_url": f"{_API}/issues/7",
        }
    )
    edited = sync_tracker(store, tracker_source, github)
    after = _rows_by_path(session_factory, tracker_source.id)
    assert (edited.indexed, edited.deleted) == (1, 1)
    assert after["pulls/9"] == before["pulls/9"]
    assert after["issues/7"] != before["issues/7"]

    github.pages["pulls"] = []
    pruned = sync_tracker(store, tracker_source, github)
    assert pruned.deleted == 1
    assert set(_rows_by_path(session_factory, tracker_source.id)) == {"issues/7"}


def test_tracker_sync_refuses_a_source_that_is_not_a_github_tracker(
    store: PgVectorStore,
    session_factory: sessionmaker[Session],
    source_id: uuid.UUID,
) -> None:
    with session_factory() as session:
        repo_source = session.get(KnowledgeSource, source_id)
    assert repo_source is not None
    with pytest.raises(ValueError, match="tracker"):
        sync_tracker(store, repo_source, _FakeGitHub())


@pytest.mark.parametrize(
    "uri", ["https://github.com/acme/..", "https://github.com/acme/widgets/../../orgs/x", "acme"]
)
def test_tracker_sync_refuses_a_uri_that_is_not_one_repository(
    store: PgVectorStore, tracker_source: KnowledgeSource, uri: str
) -> None:
    github = _FakeGitHub()
    with pytest.raises(ValueError, match="owner"):
        sync_tracker(store, KnowledgeSource(**{**_columns(tracker_source), "uri": uri}), github)
    assert github.calls == []


def _columns(source: KnowledgeSource) -> dict[str, Any]:
    return {k: getattr(source, k) for k in ("id", "workspace_id", "kind", "name", "config")}
