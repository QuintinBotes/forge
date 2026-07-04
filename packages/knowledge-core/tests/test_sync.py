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
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from forge_contracts.dtos import KnowledgeScope
from forge_contracts.enums import SyncMode
from forge_db.base import Base
from forge_db.models import KnowledgeSource, RetrievalChunk, Workspace
from forge_db.session import create_session_factory
from forge_knowledge.embeddings import DeterministicEmbeddingClient
from forge_knowledge.stores import PgVectorStore
from forge_knowledge.sync import (
    ChangeSet,
    full_sync,
    git_changed_files,
    incremental_sync,
    iter_source_files,
    read_repo_files,
    sync_source,
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
def source_id(
    session_factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> uuid.UUID:
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
            select(RetrievalChunk).where(
                RetrievalChunk.knowledge_source_id == source_id
            )
        )
        for row in rows:
            out.setdefault(row.path or "", []).append((row.id, row.content_hash))
    return out


# --------------------------------------------------------------------------- #
# Store deletion / introspection helpers (foundation for incremental sync)     #
# --------------------------------------------------------------------------- #


def test_store_source_paths_lists_indexed_paths(
    store: PgVectorStore, source_id: uuid.UUID
) -> None:
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


def test_incremental_sync_no_changes_is_noop(
    store: PgVectorStore, source_id: uuid.UUID
) -> None:
    full_sync(store, str(source_id), CORPUS)
    result = incremental_sync(store, str(source_id), CORPUS, ChangeSet())
    assert result.indexed == 0
    assert result.deleted == 0
    assert result.updated == 0


def test_incremental_result_still_searchable(
    store: PgVectorStore, source_id: uuid.UUID
) -> None:
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
