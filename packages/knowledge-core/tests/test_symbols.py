"""Code-symbol index (spec J §2.3 / §2.4.1 D-c): symbols persisted and summarised."""

from __future__ import annotations

import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from forge_contracts.dtos import KnowledgeScope
from forge_contracts.enums import ChunkType
from forge_db.base import Base
from forge_db.models import KnowledgeSource, RetrievalChunk, Workspace
from forge_db.session import create_session_factory
from forge_knowledge.chunking import chunk_code
from forge_knowledge.stores import Bm25Store, PgVectorStore
from forge_knowledge.symbols import symbol_chunks
from forge_knowledge.sync import full_sync

SRC = '''"""Module."""
import os


def alpha(x):
    """Add one."""
    return x + 1


@deco
def beta(
    y,
    z,
):
    return y * z


class Gamma:
    """A class."""
'''


def test_symbol_chunks_summarise_each_named_symbol() -> None:
    summaries = symbol_chunks(chunk_code("app/sample.py", SRC))
    assert [c.content for c in summaries] == [
        "app/sample.py: alpha — def alpha(x):",
        "app/sample.py: beta — def beta(y, z,):",
        "app/sample.py: Gamma — class Gamma:",
    ]
    for chunk in summaries:
        assert chunk.chunk_type is ChunkType.SUMMARY
        assert chunk.metadata == {"symbol": chunk.symbol}
        assert chunk.path == "app/sample.py"
        assert chunk.weight == 1.2
        assert chunk.content_hash
    # Pure and stable: the same code always yields the same hashes.
    again = symbol_chunks(chunk_code("app/sample.py", SRC))
    assert [c.content_hash for c in again] == [c.content_hash for c in summaries]


def _source(factory: sessionmaker[Session]) -> tuple[uuid.UUID, uuid.UUID]:
    with factory() as session:
        workspace = Workspace(name="Acme", slug="acme")
        session.add(workspace)
        session.flush()
        source = KnowledgeSource(
            workspace_id=workspace.id, kind="repo", name="app", uri="github.com/org/app"
        )
        session.add(source)
        session.flush()
        ids = (workspace.id, source.id)
        session.commit()
    return ids


def test_repo_sync_persists_symbols_and_indexes_summaries_idempotently() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    workspace_id, source_id = _source(factory)
    store = PgVectorStore(factory, None)

    first = full_sync(store, str(source_id), {"app/sample.py": SRC})
    with factory() as session:
        rows = list(session.scalars(select(RetrievalChunk)))
    code = {row.chunk_metadata.get("symbol") for row in rows if row.chunk_type == ChunkType.CODE}
    assert {"alpha", "beta", "Gamma", "<module>"} <= code  # Chunk.symbol persisted
    summaries = sorted(row.content for row in rows if row.chunk_type == ChunkType.SUMMARY)
    assert summaries == sorted(c.content for c in symbol_chunks(chunk_code("app/sample.py", SRC)))

    hits = Bm25Store(factory).search("Gamma", KnowledgeScope(workspace_id=workspace_id), k=5)
    assert any(h.content == "app/sample.py: Gamma — class Gamma:" for h in hits)

    second = full_sync(store, str(source_id), {"app/sample.py": SRC})
    assert (second.indexed, second.deleted, second.skipped) == (0, 0, first.indexed)
