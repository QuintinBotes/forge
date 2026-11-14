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

A checked-out repository's sync can also index its commit history
(:func:`git_log`, one ``commits/<sha>`` item per commit; opt-in per source,
see :func:`commit_limit`), and :func:`sync_tracker` fills a GitHub tracker
source with its issues and pull requests (spec J §2.3).

The store argument only needs ``index``, ``delete_source_paths``,
``delete_stale`` and ``source_paths`` (the :class:`SyncStore` Protocol), which
:class:`forge_knowledge.stores.PgVectorStore` provides — so sync works
identically against the SQLite unit-test backend and Postgres in production.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from forge_contracts.dtos import Chunk, IndexResult
from forge_contracts.enums import KnowledgeSourceKind, SyncMode
from forge_db.models import KnowledgeSource
from forge_knowledge.chunking import DEFAULT_MAX_CHARS, chunk_file
from forge_knowledge.symbols import symbol_chunks
from forge_knowledge.tracker import CommitRecord, commit_chunks, issue_chunks, pull_chunks

__all__ = [
    "DEFAULT_EXCLUDE_DIRS",
    "DEFAULT_EXCLUDE_SUFFIXES",
    "DEFAULT_MAX_COMMITS",
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MCP_FRESHNESS_SLA_MINUTES",
    "MAX_COMMITS",
    "ChangeSet",
    "SyncStore",
    "commit_limit",
    "freshness_sla_minutes",
    "full_sync",
    "git_changed_files",
    "git_log",
    "incremental_sync",
    "is_stale",
    "is_tracker_source",
    "iter_source_files",
    "read_repo_files",
    "sync_source",
    "sync_tracker",
    "tracker_repo",
]

_log = logging.getLogger(__name__)


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

#: Commits a REPO source's sync reads when ``config.commits.max_count`` is unset.
DEFAULT_MAX_COMMITS: int = 500

#: Hard cap on ``config.commits.max_count``.
MAX_COMMITS: int = 5000


# --------------------------------------------------------------------------- #
# Store surface                                                                #
# --------------------------------------------------------------------------- #


class SyncStore(Protocol):
    """The minimal store surface sync needs (``PgVectorStore`` satisfies it)."""

    def index(
        self, source_id: str, chunks: list[Chunk], *, defer_embedding: bool = False
    ) -> IndexResult: ...

    def source_paths(self, source_id: str) -> set[str]: ...

    def delete_source_paths(self, source_id: str, paths: Iterable[str]) -> int: ...

    def delete_stale(self, source_id: str, current: Mapping[str, Collection[str]]) -> int: ...


@runtime_checkable
class BacklogStore(Protocol):
    """A store that can embed rows indexed before an embedder was configured."""

    def embed_missing(self, source_id: str) -> tuple[int, list[str]]: ...


def embed_backlog(store: object, source_id: str) -> tuple[int, list[str]]:
    """Embed ``source_id``'s NULL-embedding rows, if the store can; never raises.

    Called by every sync after its own writes (and after pruning), so newly
    indexed chunks are committed whatever the provider does with old rows.
    """
    if not isinstance(store, BacklogStore):
        return 0, []
    try:
        return store.embed_missing(source_id)
    except Exception as exc:  # the backlog retries on the next sync
        return 0, [f"embedding backlog: {type(exc).__name__}"]


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
    # Fixed argv, no shell; GIT_NO_LAZY_FETCH stops a partial clone fetching
    # missing objects from its promisor remote, so this never touches the network.
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        errors="replace",
        env={**os.environ, "GIT_NO_LAZY_FETCH": "1"},
    )
    return proc.stdout


def _check_ref(ref: str | None) -> None:
    """Refuse a revision git could read as an option (or one empty or with spaces)."""
    if ref is not None and (not ref or ref.startswith("-") or any(c.isspace() for c in ref)):
        raise ValueError(f"not a git revision: {ref!r}")


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
    _check_ref(base_ref)
    _check_ref(head_ref)
    target = base_ref if head_ref is None else f"{base_ref}..{head_ref}"
    output = runner(Path(root), ["diff", "--name-status", "-M", "--end-of-options", target])
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


# Each record opens with a NUL (an empty field), then sha, committer date,
# subject and body; `-z` ends the body with a NUL and NUL-terminates every
# `--name-only` path. No message or path can contain a NUL, so no body or
# path can forge a record.
_LOG_FORMAT = "%x00%H%x00%cI%x00%s%x00%b"
_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def git_log(
    root: str | Path,
    rev: str,
    *,
    max_count: int | None = None,
    runner: GitRunner = _run_git,
) -> Iterator[CommitRecord]:
    """Commits reachable from ``rev`` (newest first) with the paths each touched.

    Local ``git log -z --name-only`` with a fixed format; no network. A record
    that does not parse (or has an invalid date) is skipped and counted in a
    warning, never fatal.
    """
    if rev.startswith("-"):
        raise ValueError(f"not a revision: {rev!r}")
    limit = [f"--max-count={max_count}"] if max_count is not None else []
    output = runner(
        Path(root),
        [
            "log",
            "-z",
            "--no-show-signature",
            "--no-renames",
            "--name-only",
            f"--format={_LOG_FORMAT}",
            *limit,
            "--end-of-options",
            rev,
            "--",
        ],
    )
    fields = output.split("\0")
    skipped = 0
    i = 1  # fields[0] is the empty field before the first record's NUL
    while i + 3 < len(fields):
        sha, date, subject, body = fields[i : i + 4]
        i += 4
        paths: list[str] = []
        while i < len(fields) and fields[i]:
            # git puts a newline between a record's body and its first path.
            paths.append(fields[i] if paths else fields[i].removeprefix("\n"))
            i += 1
        i += 1  # the empty field that opens the next record
        try:
            if not _SHA_RE.fullmatch(sha):
                raise ValueError(sha)
            committed_at = datetime.fromisoformat(date)
        except ValueError:
            skipped += 1
            continue
        yield CommitRecord(
            sha=sha,
            committed_at=committed_at,
            subject=subject,
            body=body.strip(),
            paths=tuple(paths),
        )
    if skipped:
        _log.warning("git log %s: skipped %d unparseable commit records", rev, skipped)


def commit_limit(config: Mapping[str, Any] | None) -> int | None:
    """``--max-count`` for a REPO source's commit history, ``None`` when not enabled.

    Commit history is opt-in: ``config.commits.enabled`` must be ``true``.
    ``config.commits.max_count`` defaults to :data:`DEFAULT_MAX_COMMITS` and
    is capped at :data:`MAX_COMMITS`.
    """
    commits = (config or {}).get("commits")
    if not isinstance(commits, Mapping) or commits.get("enabled") is not True:
        return None
    count = commits.get("max_count", DEFAULT_MAX_COMMITS)
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError(f"config.commits.max_count must be a positive integer: {count!r}")
    return min(count, MAX_COMMITS)


def _history(root: Path, rev: str, max_count: int | None) -> list[Chunk]:
    """Commit chunks for ``rev`` when enabled and ``root`` is a git checkout's top."""
    if max_count is None or not (root / ".git").exists():
        return []
    try:
        _run_git(root, ["rev-parse", "--verify", "--quiet", "--end-of-options", "HEAD"])
    except subprocess.CalledProcessError:
        return []  # a repository with no commits yet has no history to index
    records = git_log(root, rev, max_count=max_count)
    return [chunk for record in records for chunk in commit_chunks(record)]


# --------------------------------------------------------------------------- #
# Sync operations                                                              #
# --------------------------------------------------------------------------- #


def _chunk_files(files: Mapping[str, str], paths: Iterable[str], max_chars: int) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in sorted(paths):
        src = files.get(path)
        if src is None:
            continue
        file_chunks = chunk_file(path, src, max_chars=max_chars)
        chunks.extend(file_chunks)
        chunks.extend(symbol_chunks(file_chunks))
    return chunks


def full_sync(
    store: SyncStore,
    source_id: str,
    files: Mapping[str, str],
    *,
    prune: bool = True,
    max_chars: int = DEFAULT_MAX_CHARS,
    extra: Sequence[Chunk] = (),
) -> IndexResult:
    """Full sync: chunk + index every file, then drop stale and vanished chunks.

    New chunks are written before any old chunk of a file is deleted, so a
    re-chunk (a changed file, or every file after a chunker change) never
    leaves the source without chunks. Idempotent: unchanged chunks are
    skipped (content-hash dedup in the store), so re-running with no file
    changes indexes and deletes nothing. ``extra`` chunks (commit history)
    are reconciled with the files' own, path by path.
    """
    chunks = [*_chunk_files(files, files.keys(), max_chars), *extra]
    return _reconcile(store, source_id, chunks, files.keys(), prune=prune)


def _reconcile(
    store: SyncStore,
    source_id: str,
    chunks: list[Chunk],
    paths: Iterable[str],
    *,
    prune: bool,
    removed: Collection[str] = (),
    replaced_as_updated: bool = False,
) -> IndexResult:
    """Write ``chunks``, then drop stale chunks at their ``paths``, ``removed``, orphans.

    Stale chunks of a rewritten path count as ``deleted`` (``updated`` with
    ``replaced_as_updated``); chunks of removed and orphaned paths as ``deleted``.
    """
    indexed = store.index(source_id, chunks)

    current: dict[str, set[str]] = {path: set() for path in paths}
    for chunk in chunks:
        content_hash = chunk.content_hash or hashlib.sha256(chunk.content.encode()).hexdigest()
        current.setdefault(chunk.path or "", set()).add(content_hash)
    replaced = store.delete_stale(source_id, current)
    gone = set(removed) - set(current)
    deleted = store.delete_source_paths(source_id, gone) if gone else 0
    if prune:
        orphans = store.source_paths(source_id) - set(current)
        if orphans:
            deleted += store.delete_source_paths(source_id, orphans)
    rewritten = 0
    errors = list(indexed.errors)
    if replaced or deleted:
        # Dedup is by hash across the source: a chunk that moved between files
        # was skipped against a row just deleted, so write it back. No provider
        # call between that delete and this write (the backlog below embeds),
        # so a provider outage cannot leave a renamed file without chunks.
        again = store.index(source_id, chunks, defer_embedding=True)
        rewritten = again.indexed
        errors.extend(again.errors)

    # After pruning, so a vanished file's text is never sent to the provider.
    embedded, backlog_errors = embed_backlog(store, source_id)
    if not replaced_as_updated:
        deleted, replaced = deleted + replaced, 0
    return IndexResult(
        source_id=str(source_id),
        indexed=indexed.indexed + rewritten,
        updated=indexed.updated + embedded + replaced,
        deleted=deleted,
        skipped=indexed.skipped - rewritten,
        errors=[*errors, *backlog_errors],
    )


def incremental_sync(
    store: SyncStore,
    source_id: str,
    files: Mapping[str, str],
    changes: ChangeSet,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    extra: Sequence[Chunk] = (),
) -> IndexResult:
    """Incremental sync: re-index only ``changes``; leave everything else alone.

    ``files`` must supply current content for every changed path. Modified files
    have their old chunks replaced (counted in ``updated``); deleted / renamed-away
    files have their chunks removed (counted in ``deleted``). ``extra`` chunks
    (the new commits) are only added. A changed file's new chunks are written
    before its old ones are deleted, so a failed write leaves the old ones.
    """
    changed = changes.changed_paths
    chunks = [*_chunk_files(files, changed, max_chars), *extra]
    return _reconcile(
        store,
        source_id,
        chunks,
        changed,
        prune=False,
        removed=changes.removed_paths,
        replaced_as_updated=True,
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
    max_commits: int | None = None,
) -> IndexResult:
    """Drive a sync from a checked-out repository ``root``.

    * ``FULL`` / ``SYNC_AND_INDEX``: read the whole tree and :func:`full_sync`.
    * ``INCREMENTAL``: diff ``base_ref``..``head_ref`` with git, then re-index
      only the changed files via :func:`incremental_sync`.

    With ``max_commits`` (see :func:`commit_limit`) and ``root`` a git
    checkout, up to that many commits (of ``HEAD`` on a full sync,
    ``base_ref..head_ref`` on an incremental one) are indexed too.
    """
    _check_ref(base_ref)
    _check_ref(head_ref)
    root = Path(root)
    if mode in (SyncMode.FULL, SyncMode.SYNC_AND_INDEX):
        history = _history(root, "HEAD", max_commits)
        return full_sync(store, source_id, read_repo_files(root), prune=prune, extra=history)

    if mode == SyncMode.INCREMENTAL:
        if not base_ref:
            raise ValueError("incremental sync requires a base_ref (the git ref to diff from)")
        changes = git_changed_files(root, base_ref, head_ref)
        history = _history(root, f"{base_ref}..{head_ref or 'HEAD'}", max_commits)
        return incremental_sync(store, source_id, read_repo_files(root), changes, extra=history)

    raise ValueError(f"unsupported sync mode for filesystem source: {mode}")


# --------------------------------------------------------------------------- #
# Tracker sync (spec J §2.3)                                                   #
# --------------------------------------------------------------------------- #


class PageClient(Protocol):
    """The paginated read tracker sync needs (``forge_integrations.GitHubClient``)."""

    def iter_pages(
        self, repo: str, path: str, params: Mapping[str, str] | None = None
    ) -> Iterator[dict[str, Any]]: ...


# `owner/repo` as GitHub allows them; never `..`, so the uri cannot reach another endpoint.
_GITHUB_REPO_RE = re.compile(r"[A-Za-z0-9-]+/(?!\.\.?$)[A-Za-z0-9_.-]+")
_ITEMS = {"state": "all", "sort": "created", "direction": "asc", "per_page": "100"}
_COMMENTS = {"sort": "created", "direction": "asc", "per_page": "100"}


def is_tracker_source(source: KnowledgeSource) -> bool:
    """Whether ``source`` is a GitHub tracker (``kind=URL``, ``config.tracker == "github"``)."""
    tracker = (source.config or {}).get("tracker")
    return source.kind == KnowledgeSourceKind.URL and tracker == "github"


# --------------------------------------------------------------------------- #
# Freshness (one rule for the MCP index status and the knowledge page)         #
# --------------------------------------------------------------------------- #

#: The freshness SLA of an MCP source that sets none of its own.
DEFAULT_MCP_FRESHNESS_SLA_MINUTES = 30


def freshness_sla_minutes(source: KnowledgeSource) -> int | None:
    """The SLA ``source`` is held to: its own, else 30 minutes for an MCP
    source, else none (a non-MCP source is stale only past an explicit SLA)."""
    if source.kind == KnowledgeSourceKind.MCP:
        return source.freshness_sla_minutes or DEFAULT_MCP_FRESHNESS_SLA_MINUTES
    return source.freshness_sla_minutes


def is_stale(source: KnowledgeSource, now: datetime) -> bool:
    """Whether ``source`` is past its SLA (never synced counts); no SLA, never stale."""
    sla = freshness_sla_minutes(source)
    if sla is None:
        return False
    last = source.last_synced_at
    if last is None:
        return True
    if last.tzinfo is None:  # stored as naive UTC
        last = last.replace(tzinfo=UTC)
    return (now - last).total_seconds() > sla * 60


def tracker_repo(uri: str) -> str:
    """``owner/repo`` from a tracker source's ``https://github.com/<owner>/<repo>`` uri."""
    repo = uri.removeprefix("https://github.com/").removesuffix(".git").strip("/")
    if not _GITHUB_REPO_RE.fullmatch(repo):
        raise ValueError(f"tracker uri is not https://github.com/<owner>/<repo>: {uri!r}")
    return repo


def _by_number(items: Iterable[Mapping[str, Any]], url_key: str) -> dict[int, list[Any]]:
    """Group comments by the issue / PR number that ends their ``url_key``."""
    grouped: dict[int, list[Any]] = defaultdict(list)
    for item in items:
        grouped[int(str(item[url_key]).rsplit("/", 1)[1])].append(item)
    return grouped


def sync_tracker(
    store: SyncStore,
    source: KnowledgeSource,
    client: PageClient,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> IndexResult:
    """Full sync of a ``KnowledgeSource(kind=URL, config={"tracker": "github"})``.

    Reads the repository named by ``source.uri`` through ``client`` in four
    repo-wide listings (issues, pull requests, conversation comments, review
    comments), maps each issue and PR with :mod:`forge_knowledge.tracker`
    (``issues/<n>``, ``pulls/<n>``) and reconciles them like a full file sync:
    unchanged items are skipped by content hash, an edited item's old pieces
    are replaced after its new ones are written, and a vanished item is pruned.
    Every listing is read before anything is written, so a failed or
    over-long listing changes nothing.
    """
    if not is_tracker_source(source):
        raise ValueError("tracker sync needs a URL source with config.tracker == 'github'")
    repo = tracker_repo(source.uri)
    comments = _by_number(client.iter_pages(repo, "issues/comments", _COMMENTS), "issue_url")
    reviews = _by_number(client.iter_pages(repo, "pulls/comments", _COMMENTS), "pull_request_url")
    chunks: list[Chunk] = []
    for issue in client.iter_pages(repo, "issues", _ITEMS):
        if "pull_request" not in issue:  # the issues listing includes PRs
            chunks += issue_chunks(issue, comments[issue["number"]], max_chars=max_chars)
    for pull in client.iter_pages(repo, "pulls", _ITEMS):
        number = pull["number"]
        chunks += pull_chunks(pull, comments[number], reviews[number], max_chars=max_chars)
    return _reconcile(store, str(source.id), chunks, (), prune=True)
