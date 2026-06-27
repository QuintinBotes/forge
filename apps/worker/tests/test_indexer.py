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


def test_index_source_is_idempotent(
    service: KnowledgeService, source_id: uuid.UUID
) -> None:
    first = index_source(service, str(source_id), FILES)
    second = index_source(service, str(source_id), FILES)
    assert first.indexed > 0
    assert second.indexed == 0
    assert second.skipped == first.indexed


def test_celery_task_is_registered() -> None:
    from forge_worker.celery_app import celery_app

    assert "forge.knowledge.index_source" in celery_app.tasks
    assert index_source_task.name == "forge.knowledge.index_source"
