"""Knowledge source sync modes (plan Task 1.4, RAG spine).

This is the *ingestion* half of the spine: it keeps a knowledge source's indexed
chunks in step with its files. Two modes mirror the spec's *Knowledge Sync Modes*
table:

* :func:`full_sync` — (re)chunk and index **every** file. Idempotent by content
  hash (unchanged chunks are skipped, not rewritten); with ``prune=True`` it also
  deletes the chunks of files that no longer exist, so a full sync fully
  reconciles the index to the current tree.

* :func:`incremental_sync` — given a :class:`ChangeSet` (typically from
  :func:`git_changed_files`), it re-indexes **only** the changed files and drops
  the chunks of deleted / renamed-away files. Every untouched file's chunks are
  left exactly as they were — the property that makes incremental sync cheap.

:func:`sync_source` is the filesystem + git glue that drives either mode from a
checked-out repository path. All git access is local ``git diff`` (no network);
file reading skips binaries, VCS metadata, and oversized blobs.

The store argument only needs ``index`` + ``delete_source_paths`` + ``source_paths``
(the :class:`SyncStore` Protocol), which :class:`forge_knowledge.stores.PgVectorStore`
provides — so sync works identically against the SQLite unit-test backend and
Postgres in production.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from forge_contracts.dtos import Chunk, IndexResult
from forge_contracts.enums import SyncMode
from forge_knowledge.chunking import DEFAULT_MAX_CHARS, chunk_file

__all__ = [
    "DEFAULT_EXCLUDE_DIRS",
    "DEFAULT_EXCLUDE_SUFFIXES",
    "DEFAULT_MAX_FILE_BYTES",
    "ChangeSet",
    "SyncStore",
    "full_sync",
    "git_changed_files",
    "incremental_sync",
    "iter_source_files",
    "read_repo_files",
    "sync_source",
]


# Directories never indexed (VCS metadata, dependency/build caches, virtualenvs).
DEFAULT_EXCLUDE_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".idea",
        ".vscode",
        "dist",
        "build",
        ".next",
        ".turbo",
        "target",
    }
)

# Binary / non-source suffixes that carry no useful retrieval text.
DEFAULT_EXCLUDE_SUFFIXES: frozenset[str] = frozenset(
    {
        ".pyc",
        ".pyo",
        ".so",
        ".o",
        ".a",
        ".dylib",
        ".dll",
        ".class",
        ".jar",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".ico",
        ".svg",
        ".webp",
        ".pdf",
        ".zip",
        ".gz",
        ".tar",
        ".tgz",
        ".bz2",
        ".7z",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".mp4",
        ".mov",
        ".mp3",
        ".wav",
        ".bin",
        ".wasm",
        ".lock",
    }
)

# Skip blobs larger than this (bytes): minified bundles, vendored data, etc.
DEFAULT_MAX_FILE_BYTES: int = 1_000_000


# --------------------------------------------------------------------------- #
# Store surface                                                                #
# --------------------------------------------------------------------------- #


class SyncStore(Protocol):
    """The minimal store surface sync needs (``PgVectorStore`` satisfies it)."""

    def index(self, source_id: str, chunks: list[Chunk]) -> IndexResult: ...

    def source_paths(self, source_id: str) -> set[str]: ...

    def delete_source_paths(self, source_id: str, paths: Iterable[str]) -> int: ...


# --------------------------------------------------------------------------- #
# Change set                                                                   #
# --------------------------------------------------------------------------- #


@dataclass
class ChangeSet:
    """A parsed set of file changes between two revisions.

    ``renamed`` holds ``(old_path, new_path)`` pairs. ``changed_paths`` are the
    files that must be (re)indexed; ``removed_paths`` are the files whose chunks
    must be deleted.
    """

    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    renamed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def changed_paths(self) -> set[str]:
        """Files needing a (re)index: added, modified, and rename destinations."""
        return set(self.added) | set(self.modified) | {new for _, new in self.renamed}

    @property
    def removed_paths(self) -> set[str]:
        """Files whose chunks must be deleted: deleted, and rename sources."""
        return set(self.deleted) | {old for old, _ in self.renamed}

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.modified or self.deleted or self.renamed)


# --------------------------------------------------------------------------- #
# Filesystem walking                                                           #
# --------------------------------------------------------------------------- #


def iter_source_files(
    root: str | Path,
    *,
    exclude_dirs: Iterable[str] = DEFAULT_EXCLUDE_DIRS,
    exclude_suffixes: Iterable[str] = DEFAULT_EXCLUDE_SUFFIXES,
    max_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> Iterator[tuple[str, str]]:
    """Yield ``(relative_posix_path, utf-8 text)`` for each indexable file.

    Directories in ``exclude_dirs``, files with an excluded suffix, files over
    ``max_bytes``, and anything not decodable as UTF-8 are skipped. Output is
    deterministic (sorted) so repeated syncs are stable.
    """
    root_path = Path(root)
    exclude_dir_set = frozenset(exclude_dirs)
    exclude_suffix_set = frozenset(s.lower() for s in exclude_suffixes)

    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = sorted(d for d in dirnames if d not in exclude_dir_set)
        for filename in sorted(filenames):
            if Path(filename).suffix.lower() in exclude_suffix_set:
                continue
            abs_path = Path(dirpath) / filename
            try:
                if abs_path.stat().st_size > max_bytes:
                    continue
                text = abs_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            yield abs_path.relative_to(root_path).as_posix(), text


def read_repo_files(
    root: str | Path,
    *,
    exclude_dirs: Iterable[str] = DEFAULT_EXCLUDE_DIRS,
    exclude_suffixes: Iterable[str] = DEFAULT_EXCLUDE_SUFFIXES,
    max_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> dict[str, str]:
    """Read an entire source tree into a ``{relative_path: text}`` mapping."""
    return dict(
        iter_source_files(
            root,
            exclude_dirs=exclude_dirs,
            exclude_suffixes=exclude_suffixes,
            max_bytes=max_bytes,
        )
    )


# --------------------------------------------------------------------------- #
# git diff parsing                                                             #
# --------------------------------------------------------------------------- #

#: A git runner: ``(repo_root, args) -> stdout``. Injectable for hermetic tests.
GitRunner = Callable[[Path, list[str]], str]


def _run_git(root: Path, args: list[str]) -> str:
    """Run ``git -C <root> <args>`` locally and return stdout (no network)."""
    # Fixed argv, no shell; local git only (no network) — safe by construction.
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout


def git_changed_files(
    root: str | Path,
    base_ref: str,
    head_ref: str | None = None,
    *,
    runner: GitRunner = _run_git,
) -> ChangeSet:
    """Parse ``git diff --name-status`` between ``base_ref`` and ``head_ref``.

    With ``head_ref=None`` the diff is ``base_ref`` against the working tree (for
    tracked files). Renames are detected (``-M``) and reported as ``(old, new)``.
    """
    target = base_ref if head_ref is None else f"{base_ref}..{head_ref}"
    output = runner(Path(root), ["diff", "--name-status", "-M", target])
    return _parse_name_status(output)


def _parse_name_status(output: str) -> ChangeSet:
    changes = ChangeSet()
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        code = parts[0][:1]
        if code == "A" and len(parts) >= 2:
            changes.added.append(parts[1])
        elif code in ("M", "T") and len(parts) >= 2:
            changes.modified.append(parts[1])
        elif code == "D" and len(parts) >= 2:
            changes.deleted.append(parts[1])
        elif code == "R" and len(parts) >= 3:
            changes.renamed.append((parts[1], parts[2]))
        elif code == "C" and len(parts) >= 3:
            # A copy: the destination is new content to index.
            changes.added.append(parts[2])
    return changes


# --------------------------------------------------------------------------- #
# Sync operations                                                              #
# --------------------------------------------------------------------------- #


def _chunk_files(files: Mapping[str, str], paths: Iterable[str], max_chars: int) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in sorted(paths):
        src = files.get(path)
        if src is None:
            continue
        chunks.extend(chunk_file(path, src, max_chars=max_chars))
    return chunks


def full_sync(
    store: SyncStore,
    source_id: str,
    files: Mapping[str, str],
    *,
    prune: bool = True,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> IndexResult:
    """Full sync: chunk + index every file; prune chunks of vanished files.

    Idempotent: unchanged chunks are skipped (content-hash dedup in the store),
    so re-running with no file changes indexes nothing.
    """
    chunks = _chunk_files(files, files.keys(), max_chars)
    indexed = store.index(source_id, chunks)

    deleted = 0
    if prune:
        orphans = store.source_paths(source_id) - set(files)
        if orphans:
            deleted = store.delete_source_paths(source_id, orphans)

    return IndexResult(
        source_id=str(source_id),
        indexed=indexed.indexed,
        updated=indexed.updated,
        deleted=deleted,
        skipped=indexed.skipped,
        errors=list(indexed.errors),
    )


def incremental_sync(
    store: SyncStore,
    source_id: str,
    files: Mapping[str, str],
    changes: ChangeSet,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> IndexResult:
    """Incremental sync: re-index only ``changes``; leave everything else alone.

    ``files`` must supply current content for every changed path. Modified files
    have their old chunks replaced (counted in ``updated``); deleted / renamed-away
    files have their chunks removed (counted in ``deleted``).
    """
    changed = changes.changed_paths
    removed = changes.removed_paths

    # Replace modified files: delete their existing chunks before re-indexing.
    # (Added / rename-destination paths have no existing chunks, so this is a
    # no-op for them and `updated` reflects only genuine replacements.)
    updated = store.delete_source_paths(source_id, changed) if changed else 0
    deleted = store.delete_source_paths(source_id, removed) if removed else 0

    chunks = _chunk_files(files, changed, max_chars)
    indexed = store.index(source_id, chunks) if chunks else None

    return IndexResult(
        source_id=str(source_id),
        indexed=indexed.indexed if indexed else 0,
        updated=updated,
        deleted=deleted,
        skipped=indexed.skipped if indexed else 0,
    )


def sync_source(
    store: SyncStore,
    source_id: str,
    root: str | Path,
    *,
    mode: SyncMode = SyncMode.FULL,
    base_ref: str | None = None,
    head_ref: str | None = None,
    prune: bool = True,
) -> IndexResult:
    """Drive a sync from a checked-out repository ``root``.

    * ``FULL`` / ``SYNC_AND_INDEX``: read the whole tree and :func:`full_sync`.
    * ``INCREMENTAL``: diff ``base_ref``..``head_ref`` with git, then re-index
      only the changed files via :func:`incremental_sync`.
    """
    if mode in (SyncMode.FULL, SyncMode.SYNC_AND_INDEX):
        return full_sync(store, source_id, read_repo_files(root), prune=prune)

    if mode == SyncMode.INCREMENTAL:
        if not base_ref:
            raise ValueError("incremental sync requires a base_ref (the git ref to diff from)")
        changes = git_changed_files(root, base_ref, head_ref)
        return incremental_sync(store, source_id, read_repo_files(root), changes)

    raise ValueError(f"unsupported sync mode for filesystem source: {mode}")
