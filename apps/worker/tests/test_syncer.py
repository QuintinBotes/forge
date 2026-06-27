"""Tests for the worker syncer task (plan Task 1.4 — full + incremental sync).

Hermetic: in-memory SQLite-backed :class:`KnowledgeService`, deterministic
embedding, fixture reranker. No network, no live Redis/Celery broker.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from forge_db.base import Base
from forge_db.models import KnowledgeSource, Workspace
from forge_db.session import create_session_factory
from forge_knowledge import (
    DeterministicEmbeddingClient,
    FixtureRerankerClient,
    KnowledgeService,
)
from forge_worker.syncer import sync_files, sync_source_task


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
    "README.md": "# Service\n\nValidates oauth jwt tokens.\n",
}


def test_sync_files_indexes_then_prunes(
    service: KnowledgeService, source_id: uuid.UUID
) -> None:
    first = sync_files(service, str(source_id), FILES)
    assert first.indexed > 0

    # Drop README -> a pruning full sync removes its chunks.
    pruned = sync_files(service, str(source_id), {"auth.py": FILES["auth.py"]})
    assert pruned.deleted > 0


def test_sync_files_is_idempotent(
    service: KnowledgeService, source_id: uuid.UUID
) -> None:
    first = sync_files(service, str(source_id), FILES)
    second = sync_files(service, str(source_id), FILES)
    assert first.indexed > 0
    assert second.indexed == 0


def test_celery_task_is_registered() -> None:
    from forge_worker.celery_app import celery_app

    assert "forge.knowledge.sync_source" in celery_app.tasks
    assert sync_source_task.name == "forge.knowledge.sync_source"
