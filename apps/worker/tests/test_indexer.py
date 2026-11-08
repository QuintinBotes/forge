"""Tests for the worker indexer task (plan Task 1.3 — knowledge indexer).

The indexer is the background half of the RAG spine: it chunks a knowledge
source's files and indexes them through a :class:`KnowledgeStore`. These tests
exercise the hermetic core (``chunk_files`` + ``index_source``) against an
in-memory SQLite-backed :class:`KnowledgeService`, and assert the Celery task is
registered under its stable name — without contacting a broker.

Hermetic: in-memory SQLite, deterministic embedding, fixture reranker. No
network, no live Redis/Celery broker.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from forge_contracts.enums import ChunkType
from forge_db.base import Base
from forge_db.models import KnowledgeSource, Workspace
from forge_db.session import create_session_factory
from forge_knowledge import (
    DeterministicEmbeddingClient,
    FixtureRerankerClient,
    KnowledgeService,
)
from forge_worker.indexer import chunk_files, index_source, index_source_task


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


@pytest.fixture
def source_id(session_factory: sessionmaker[Session]) -> uuid.UUID:
    with session_factory() as session:
        workspace = Workspace(name="Acme", slug="acme")
        session.add(workspace)
        session.flush()
        source = KnowledgeSource(
            workspace_id=workspace.id, kind="repo", name="api", uri="github.com/org/api"
        )
        session.add(source)
        session.flush()
        src_id = source.id
        session.commit()
    return src_id


@pytest.fixture
def service(session_factory: sessionmaker[Session]) -> KnowledgeService:
    return KnowledgeService.from_session_factory(
        session_factory,
        DeterministicEmbeddingClient(dimension=256),
        FixtureRerankerClient(),
    )


FILES = {
    "auth.py": "def validate_jwt(token):\n    return verify(token)\n",
    "README.md": "# Service\n\nThis service validates oauth jwt tokens.\n",
}


def test_chunk_files_routes_by_extension() -> None:
    chunks = chunk_files(FILES)
    assert chunks
    by_path = {c.path for c in chunks}
    assert {"auth.py", "README.md"} <= by_path
    # Python file -> AST code chunk for the function.
    py_chunks = [c for c in chunks if c.path == "auth.py"]
    assert any(c.symbol == "validate_jwt" for c in py_chunks)
    # README markdown chunk carries the README weight (1.3x).
    md_chunks = [c for c in chunks if c.path == "README.md"]
    assert md_chunks and md_chunks[0].chunk_type == ChunkType.README


def test_index_source_indexes_and_is_searchable(
    service: KnowledgeService, source_id: uuid.UUID
) -> None:
    result = index_source(service, str(source_id), FILES)

    assert result.indexed > 0
    assert result.source_id == str(source_id)

    from forge_contracts.dtos import KnowledgeScope

    hits = service.search("validate_jwt", KnowledgeScope(), k=5)
    assert any(c.path == "auth.py" for c in hits)


def test_index_source_is_idempotent(service: KnowledgeService, source_id: uuid.UUID) -> None:
    first = index_source(service, str(source_id), FILES)
    second = index_source(service, str(source_id), FILES)
    assert first.indexed > 0
    assert second.indexed == 0
    assert second.skipped == first.indexed


def test_celery_task_is_registered() -> None:
    from forge_worker.celery_app import celery_app

    assert "forge.knowledge.index_source" in celery_app.tasks
    assert index_source_task.name == "forge.knowledge.index_source"


def _clear_embedder_cache() -> None:
    # The embedder is cached per process (a local model loads once); tests
    # that swap settings must clear it or they would observe a stale client.
    from forge_worker.indexer import get_configured_embedder

    get_configured_embedder.cache_clear()


def test_build_knowledge_service_constructs(
    monkeypatch: pytest.MonkeyPatch, session_factory: sessionmaker[Session]
) -> None:
    # ``build_knowledge_service`` resolves the default session factory at call
    # time via ``from forge_db import create_session_factory``; redirect it at the
    # source module so we get an in-memory service instead of a live Postgres one.
    monkeypatch.setattr("forge_db.create_session_factory", lambda *a, **k: session_factory)
    from forge_worker.indexer import build_knowledge_service

    _clear_embedder_cache()
    try:
        service = build_knowledge_service()
        assert isinstance(service, KnowledgeService)
    finally:
        _clear_embedder_cache()


def test_build_knowledge_service_defaults_to_honest_bm25_only(
    monkeypatch: pytest.MonkeyPatch, session_factory: sessionmaker[Session]
) -> None:
    # ADR-0018: with no embedder/reranker configured the worker must build a
    # BM25-only service (chunks stored without vectors), never the hash
    # embedder or fixture reranker. Settings are pinned explicitly so the
    # test does not depend on the developer's environment/.env.
    from forge_api.settings import Settings

    fake_settings = Settings(embedding_provider=None, rerank_provider="none")
    monkeypatch.setattr("forge_api.settings.get_settings", lambda: fake_settings)
    monkeypatch.setattr("forge_db.create_session_factory", lambda *a, **k: session_factory)
    monkeypatch.delenv("FORGE_EMBEDDING_API_KEY", raising=False)
    from forge_worker.indexer import build_knowledge_service

    _clear_embedder_cache()
    try:
        service = build_knowledge_service()
        assert service.vector_configured is False
        assert service.legs == ["bm25"]
        assert service.vector_leg == "unconfigured"
        assert service.rerank_status == "off"
    finally:
        _clear_embedder_cache()


def test_build_knowledge_service_refuses_bad_embedding_dimension(
    monkeypatch: pytest.MonkeyPatch, session_factory: sessionmaker[Session]
) -> None:
    # A dimension that does not match the vector column refuses at build time.
    from forge_api.settings import Settings

    fake_settings = Settings(
        embedding_provider="openai-compatible",
        embedding_model="text-embedding-3-small",
        embedding_dimension=384,
        rerank_provider="none",
    )
    monkeypatch.setattr("forge_api.settings.get_settings", lambda: fake_settings)
    monkeypatch.setattr("forge_db.create_session_factory", lambda *a, **k: session_factory)
    from forge_worker.indexer import build_knowledge_service

    _clear_embedder_cache()
    try:
        with pytest.raises(ValueError, match="does not match"):
            build_knowledge_service()
    finally:
        _clear_embedder_cache()


def test_worker_init_hook_refuses_bad_embedder_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ADR-0018 (correction item 8): the worker_init receiver builds the
    # embedder at worker startup and translates a factory refusal into
    # SystemExit (Celery signal dispatch swallows plain Exception), instead
    # of every knowledge task failing forever.
    from forge_api.settings import Settings
    from forge_worker.celery_app import _init_worker_embedder

    fake_settings = Settings(
        embedding_provider="openai-compatible",
        embedding_model="text-embedding-3-small",
        embedding_dimension=768,
        rerank_provider="none",
    )
    monkeypatch.setattr("forge_api.settings.get_settings", lambda: fake_settings)
    _clear_embedder_cache()
    try:
        with pytest.raises(SystemExit, match="does not match"):
            _init_worker_embedder()
    finally:
        _clear_embedder_cache()


def test_worker_init_hook_refuses_the_test_only_reranker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from forge_api.settings import Settings
    from forge_worker.celery_app import _init_worker_embedder

    fake_settings = Settings(rerank_provider="fixture")
    monkeypatch.setattr("forge_api.settings.get_settings", lambda: fake_settings)
    _clear_embedder_cache()
    try:
        with pytest.raises(SystemExit, match="test-only"):
            _init_worker_embedder()
    finally:
        _clear_embedder_cache()


def test_worker_embedder_is_cached_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    # A local model must load once per process, not per task: two calls
    # return the identical client object.
    from forge_api.settings import Settings
    from forge_worker.indexer import get_configured_embedder

    fake_settings = Settings(
        embedding_provider="openai-compatible",
        embedding_model="text-embedding-3-small",
        embedding_dimension=1536,
        rerank_provider="none",
        # SSRF allowlist keeps the URL validator off the network (hermetic).
        outbound_allowlist=["api.openai.com"],
    )
    monkeypatch.setattr("forge_api.settings.get_settings", lambda: fake_settings)
    _clear_embedder_cache()
    try:
        first = get_configured_embedder()
        assert first is not None
        assert get_configured_embedder() is first
    finally:
        _clear_embedder_cache()


def test_index_source_task_delegates(
    monkeypatch: pytest.MonkeyPatch,
    service: KnowledgeService,
    source_id: uuid.UUID,
) -> None:
    monkeypatch.setattr("forge_worker.indexer.build_knowledge_service", lambda: service)
    result = index_source_task(str(source_id), FILES)
    assert isinstance(result, dict)
    assert result["indexed"] > 0
    assert result["source_id"] == str(source_id)
