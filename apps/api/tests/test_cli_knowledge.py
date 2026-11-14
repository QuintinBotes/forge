"""``forge-cli knowledge rebuild-bm25-stats`` tests (spec J correction pass).

The operator repair command recomputes a workspace's BM25 statistics from
chunk content. Exercised end-to-end through ``cli.main`` against an
in-memory SQLite factory injected at the CLI's session-factory seam:
corrupted statistics go in, ``main`` returns 0, exact statistics come out.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from forge_api import cli as cli_mod
from forge_contracts.dtos import Chunk
from forge_db.base import Base
from forge_db.models import (
    KnowledgeSource,
    RetrievalTermStat,
    RetrievalWorkspaceStat,
    Workspace,
)
from forge_db.session import create_session_factory
from forge_knowledge.stores import PgVectorStore


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def test_knowledge_rebuild_bm25_stats_cli(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with session_factory() as session:
        workspace = Workspace(name="Acme", slug="acme")
        session.add(workspace)
        session.flush()
        workspace_id = workspace.id
        source = KnowledgeSource(
            workspace_id=workspace_id, kind="repo", name="api", uri="github.com/org/api"
        )
        session.add(source)
        session.flush()
        source_id = source.id
        session.commit()

    PgVectorStore(session_factory, None).index(
        source_id=str(source_id),
        chunks=[
            Chunk(content="retry retry timeout", path="retry.py"),
            Chunk(content="single retry mention", path="misc.py"),
        ],
    )

    # Drift: totals and df no longer describe the chunks.
    with session_factory() as session:
        totals = session.scalar(select(RetrievalWorkspaceStat))
        assert totals is not None
        totals.chunk_count = 999
        totals.total_tokens = 999
        retry_row = session.scalar(
            select(RetrievalTermStat).where(RetrievalTermStat.term == "retry")
        )
        assert retry_row is not None
        retry_row.df = 42
        session.commit()

    monkeypatch.setattr(cli_mod, "_db_session_factory", lambda: session_factory)
    assert cli_mod.main(["knowledge", "rebuild-bm25-stats", str(workspace_id)]) == 0

    with session_factory() as session:
        totals = session.scalar(select(RetrievalWorkspaceStat))
        assert totals is not None
        assert totals.chunk_count == 2
        assert totals.total_tokens == 6
        retry_df = session.scalar(
            select(RetrievalTermStat.df).where(RetrievalTermStat.term == "retry")
        )
        assert retry_df == 2


def test_knowledge_group_is_in_parser() -> None:
    help_text = cli_mod.build_parser().format_help()
    assert "knowledge" in help_text
