"""Build a *real* eval corpus from the live repo (HARD-04, honest eval).

This is the corpus half of the honest real-corpus eval. Instead of the hand-built
``retrieval_eval.SAMPLE_CORPUS`` (whose answers were authored next to the
questions), :func:`build_repo_corpus` reads the actual Forge monorepo through the
production ingestion path (:func:`forge_knowledge.sync.iter_source_files`, which
already applies the dir/suffix/size excludes) and filters it to a heterogeneous
slice (Python under ``packages``/``apps``, markdown under ``docs``, READMEs,
``examples``).

Two defence-in-depth secret guarantees (F05 AC11 / HARD-04 AC14):

* **Never ingest a secret file.** ``.env*``, ``*.pem``, ``*.key`` and
  ``deploy/secrets/**`` are excluded up front (on top of the sync ``.git`` etc.).
* **Redact any secret that slips through.** Every file's text passes the shared
  :func:`forge_knowledge.redaction.redact_secrets` filter, and every chunk is
  redacted again immediately before persistence, so no real key can reach a
  chunk, scorecard, or the eval report.

:func:`build_real_indexed_service` indexes the corpus through the byte-for-byte
production :class:`~forge_knowledge.KnowledgeService` (default in-memory SQLite,
which accepts any embedding dim via the JSON variant; an optional Postgres
``session_factory`` for the pgvector-backed variant).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from sqlalchemy import create_engine

from forge_contracts.dtos import Chunk, KnowledgeScope
from forge_contracts.protocols import EmbeddingClient, RerankerClient
from forge_db.base import Base
from forge_db.models import KnowledgeSource, Workspace
from forge_db.session import create_session_factory
from forge_knowledge import (
    KnowledgeService,
    chunk_file,
    redact_secrets,
)
from forge_knowledge.stores import SessionFactory
from forge_knowledge.sync import (
    DEFAULT_MAX_FILE_BYTES,
    read_repo_files,
)

__all__ = [
    "DEFAULT_INCLUDE_GLOBS",
    "SECRET_EXCLUDE_GLOBS",
    "build_real_indexed_service",
    "build_repo_corpus",
    "repo_root",
]

#: The heterogeneous real-corpus selection (spec §3.2 / §4 include globs).
DEFAULT_INCLUDE_GLOBS: tuple[str, ...] = (
    "packages/**/*.py",
    "apps/**/*.py",
    "docs/**/*.md",
    "**/README.md",
    "examples/**/*",
)

#: Secret-bearing paths never admitted to the corpus (defence-in-depth; AC14).
SECRET_EXCLUDE_GLOBS: tuple[str, ...] = (
    "**/.env",
    "**/.env.*",
    ".env",
    ".env.*",
    "**/*.pem",
    "**/*.key",
    "deploy/secrets/**",
    "**/secrets/**",
)


def repo_root() -> Path:
    """Best-effort locate the monorepo root (the dir holding ``pyproject.toml``).

    Walks up from this file; falls back to four levels up (``packages/evaluation/
    forge_eval`` → repo root) if no marker is found.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file() and (parent / "packages").is_dir():
            return parent
    return here.parents[3]


def _matches_any(rel: str, globs: Sequence[str]) -> bool:
    path = PurePosixPath(rel)
    return any(path.full_match(pattern) for pattern in globs)


def build_repo_corpus(
    root: str | Path | None = None,
    *,
    include_globs: Sequence[str] = DEFAULT_INCLUDE_GLOBS,
    exclude_globs: Sequence[str] = (),
    max_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> dict[str, str]:
    """Read the real repo into a ``{relative_posix_path: redacted_text}`` corpus.

    Files are read via the production ingestion walk (binaries / VCS / oversized
    blobs already skipped), then kept only if they match ``include_globs`` and do
    not match ``exclude_globs`` or the built-in :data:`SECRET_EXCLUDE_GLOBS`.
    Every returned value is passed through :func:`redact_secrets`.
    """
    resolved = repo_root() if root is None else Path(root)
    excludes = tuple(exclude_globs) + SECRET_EXCLUDE_GLOBS
    raw = read_repo_files(resolved, max_bytes=max_bytes)
    corpus: dict[str, str] = {}
    for rel, text in raw.items():
        if not _matches_any(rel, include_globs):
            continue
        if _matches_any(rel, excludes):
            continue
        corpus[rel] = redact_secrets(text)
    return dict(sorted(corpus.items()))


def build_real_indexed_service(
    corpus: dict[str, str],
    embedding_client: EmbeddingClient,
    reranker: RerankerClient,
    *,
    session_factory: SessionFactory | None = None,
    workspace_slug: str = "eval",
    source_uri: str = "github.com/forge/forge",
) -> tuple[KnowledgeService, KnowledgeScope]:
    """Index ``corpus`` through the real pipeline; return service + search scope.

    Mirrors :func:`forge_eval.retrieval_eval.build_indexed_service` but is
    corpus-driven and embedder/reranker-injected. Defaults to hermetic in-memory
    SQLite (dim-agnostic JSON embedding column); pass a Postgres ``session_factory``
    for the pgvector-backed variant. Every chunk's content is redacted immediately
    before persistence (AC14).
    """
    if session_factory is None:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        session_factory = create_session_factory(engine)

    with session_factory() as session:
        workspace = Workspace(name="Eval", slug=workspace_slug)
        session.add(workspace)
        session.flush()
        workspace_id = workspace.id
        source = KnowledgeSource(
            workspace_id=workspace_id,
            kind="repo",
            name="forge",
            uri=source_uri,
        )
        session.add(source)
        session.flush()
        source_id = source.id
        session.commit()

    service = KnowledgeService.from_session_factory(session_factory, embedding_client, reranker)

    chunks: list[Chunk] = []
    for path, src in corpus.items():
        for chunk in chunk_file(path, src):
            # Defence-in-depth: redact chunk content before it is persisted, so a
            # secret can never reach a RetrievalChunk row / scorecard / report.
            # Clear the pre-redaction hash so the store re-derives it (and dedups)
            # from the redacted content that is actually written.
            redacted = redact_secrets(chunk.content)
            if redacted != chunk.content:
                chunk.content = redacted
                chunk.content_hash = None
            chunks.append(chunk)
    service.index(str(source_id), chunks)
    return service, KnowledgeScope(workspace_id=workspace_id)
