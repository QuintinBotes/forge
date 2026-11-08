"""Integration tests for the knowledge router (Task 1.3 fills ``/knowledge/*``).

These exercise the real handlers wired to a :class:`KnowledgeService` backed by
in-memory SQLite (via dependency override), proving the route layer:

* indexes chunks through ``POST /knowledge/index``;
* runs the full hybrid pipeline through ``POST /knowledge/search`` and returns
  attributed, reranked :class:`RetrievedChunk` JSON;
* full-syncs a source from inline files and prunes vanished files through
  ``POST /knowledge/sync`` (Task 1.4).

Hermetic: in-memory SQLite (StaticPool so the app's worker thread shares the
connection), deterministic embedding, fixture reranker. No network.
"""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from forge_api.main import create_app
from forge_api.routers.knowledge import (
    get_knowledge_service,
    get_knowledge_session_factory,
)
from forge_contracts import Chunk
from forge_db.base import Base
from forge_db.models import KnowledgeSource, RetrievalChunk, Workspace
from forge_db.session import create_session_factory
from forge_knowledge import (
    DeterministicEmbeddingClient,
    FixtureRerankerClient,
    GracefulReranker,
    JinaRerankerClient,
    KnowledgeService,
)

WORKSPACE_ID = "00000000-0000-0000-0000-0000000000a1"


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


@pytest.fixture
def source_id(session_factory: sessionmaker[Session]) -> uuid.UUID:
    with session_factory() as session:
        workspace = Workspace(id=uuid.UUID(WORKSPACE_ID), name="Acme", slug="acme")
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


@pytest.fixture
def client(
    service: KnowledgeService,
    session_factory: sessionmaker[Session],
    authenticate_app: Callable[..., FastAPI],
) -> Iterator[TestClient]:
    app = create_app()
    authenticate_app(app)  # principal authenticated in WORKSPACE_ID
    app.dependency_overrides[get_knowledge_service] = lambda: service
    app.dependency_overrides[get_knowledge_session_factory] = lambda: session_factory
    with TestClient(app) as c:
        yield c


def _seed_other_workspace(
    session_factory: sessionmaker[Session],
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a second tenant (workspace + source) and return their ids."""
    other_ws = uuid.uuid4()
    with session_factory() as session:
        session.add(Workspace(id=other_ws, name="Other", slug=f"other-{other_ws.hex[:8]}"))
        session.flush()
        source = KnowledgeSource(
            workspace_id=other_ws, kind="repo", name="other", uri="github.com/other/secret"
        )
        session.add(source)
        session.flush()
        other_src_id = source.id
        session.commit()
    return other_ws, other_src_id


def _index_payload(source_id: uuid.UUID) -> dict[str, object]:
    return {
        "source_id": str(source_id),
        "chunks": [
            {"content": "def validate_jwt(token): verify oauth2 signature", "path": "auth.py"},
            {"content": "def connect_postgres(): pooled database connection", "path": "db.py"},
            {"content": "def compute_rrf_score(): reciprocal rank fusion", "path": "rank.py"},
        ],
    }


def test_index_then_search_returns_attributed_chunks(
    client: TestClient, source_id: uuid.UUID
) -> None:
    indexed = client.post("/knowledge/index", json=_index_payload(source_id))
    assert indexed.status_code == 200, indexed.text
    assert indexed.json()["indexed"] == 3

    resp = client.post(
        "/knowledge/search",
        json={
            "query": "validate an oauth jwt token",
            "scope": {"workspace_id": WORKSPACE_ID},
            "k": 3,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # ADR-0018 envelope: the injected deterministic embedder + fixture reranker
    # are *configured* legs, and the response says so.
    assert body["legs"] == ["bm25", "vector"]
    assert body["vector_leg"] == "configured"
    assert body["rerank"] == "fixture"
    results = body["results"]
    assert results
    assert results[0]["path"] == "auth.py"
    assert results[0]["source_id"] == str(source_id)
    assert results[0]["source_uri"] == "github.com/org/api"
    assert results[0]["rerank_score"] is not None


def test_search_recovers_exact_identifier(client: TestClient, source_id: uuid.UUID) -> None:
    client.post("/knowledge/index", json=_index_payload(source_id))
    resp = client.post(
        "/knowledge/search",
        json={"query": "compute_rrf_score", "scope": {"workspace_id": WORKSPACE_ID}, "k": 3},
    )
    assert resp.status_code == 200, resp.text
    assert any(c["path"] == "rank.py" for c in resp.json()["results"])


def test_search_empty_index_returns_empty_results(client: TestClient) -> None:
    resp = client.post(
        "/knowledge/search",
        json={"query": "anything", "scope": {"workspace_id": WORKSPACE_ID}, "k": 5},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"] == []
    assert resp.json()["legs"] == ["bm25", "vector"]


def test_sync_full_indexes_inline_files(client: TestClient, source_id: uuid.UUID) -> None:
    resp = client.post(
        "/knowledge/sync",
        json={
            "source_id": str(source_id),
            "mode": "full",
            "files": {
                "auth/jwt.py": "def validate_jwt(token):\n    return verify(token)\n",
                "db/pool.py": "def connect_postgres():\n    return pool()\n",
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["indexed"] >= 2
    assert body["deleted"] == 0

    found = client.post(
        "/knowledge/search",
        json={"query": "validate_jwt", "scope": {"workspace_id": WORKSPACE_ID}, "k": 3},
    )
    assert any(c["path"] == "auth/jwt.py" for c in found.json()["results"])


def test_sync_full_prunes_vanished_files(client: TestClient, source_id: uuid.UUID) -> None:
    base = {
        "source_id": str(source_id),
        "mode": "full",
        "files": {
            "a.py": "def a():\n    return 1\n",
            "b.py": "def b():\n    return 2\n",
        },
    }
    assert client.post("/knowledge/sync", json=base).status_code == 200

    # Re-sync with b.py removed: it must be pruned, a.py left untouched (skipped).
    second = client.post(
        "/knowledge/sync",
        json={
            "source_id": str(source_id),
            "mode": "full",
            "files": {"a.py": "def a():\n    return 1\n"},
        },
    )
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["deleted"] >= 1
    assert body["indexed"] == 0  # a.py unchanged -> skipped, not re-indexed


def test_sync_incremental_requires_root_and_base_ref(
    client: TestClient, source_id: uuid.UUID
) -> None:
    resp = client.post(
        "/knowledge/sync",
        json={"source_id": str(source_id), "mode": "incremental"},
    )
    assert resp.status_code == 422


def test_sync_full_requires_files_or_root(client: TestClient, source_id: uuid.UUID) -> None:
    resp = client.post(
        "/knowledge/sync",
        json={"source_id": str(source_id), "mode": "full"},
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# Per-workspace isolation (cross-tenant data access fix)                       #
# --------------------------------------------------------------------------- #


def test_index_rejects_source_in_another_workspace(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    # The caller (WORKSPACE_ID) must not be able to write into a source owned by
    # a different tenant, even knowing its id.
    _, other_src_id = _seed_other_workspace(session_factory)
    resp = client.post(
        "/knowledge/index",
        json={"source_id": str(other_src_id), "chunks": [{"content": "x", "path": "x.py"}]},
    )
    assert resp.status_code == 404


def test_sync_rejects_source_in_another_workspace(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    _, other_src_id = _seed_other_workspace(session_factory)
    resp = client.post(
        "/knowledge/sync",
        json={"source_id": str(other_src_id), "mode": "full", "files": {"a.py": "def a(): ..."}},
    )
    assert resp.status_code == 404


def test_search_cannot_read_another_workspace(
    client: TestClient,
    source_id: uuid.UUID,
    service: KnowledgeService,
    session_factory: sessionmaker[Session],
) -> None:
    # Seed the caller's own workspace through the API.
    client.post("/knowledge/index", json=_index_payload(source_id))
    # Seed a SECOND tenant's source directly (bypassing the API's workspace gate)
    # with a chunk that would match the query.
    other_ws, other_src_id = _seed_other_workspace(session_factory)
    service.index(
        str(other_src_id),
        [Chunk(content="def validate_jwt(token): TENANT B SECRET", path="secret.py")],
    )

    # Spoof attempt: pass tenant B's workspace_id in the request scope.
    spoof = client.post(
        "/knowledge/search",
        json={
            "query": "validate an oauth jwt token",
            "scope": {"workspace_id": str(other_ws)},
            "k": 10,
        },
    )
    assert spoof.status_code == 200
    assert all(c["source_id"] != str(other_src_id) for c in spoof.json()["results"])

    # Default (empty) scope must also stay within the caller's workspace.
    default = client.post(
        "/knowledge/search",
        json={"query": "validate an oauth jwt token", "k": 10},
    )
    assert default.status_code == 200
    returned = default.json()["results"]
    assert returned  # caller's own workspace results are present
    assert all(c["source_id"] != str(other_src_id) for c in returned)


# --------------------------------------------------------------------------- #
# HARD-03: /knowledge/search survives a reranker outage (weighted-RRF)         #
# --------------------------------------------------------------------------- #


def _degraded_reranker() -> GracefulReranker:
    """A GracefulReranker over a Jina client whose upstream always 503s."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    inner = JinaRerankerClient(
        "jina-reranker-v2",
        provider="jina",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return GracefulReranker(inner, timeout_ms=800)


def test_search_survives_reranker_outage_with_weighted_rrf(
    session_factory: sessionmaker[Session],
    source_id: uuid.UUID,
    authenticate_app: Callable[..., FastAPI],
) -> None:
    # AC3 (API): a live reranker that always fails must not fail the search — the
    # route returns 200 with weighted-RRF order and every chunk's rerank_score is
    # null (the operator-visible degradation signal for the "reranker unavailable"
    # badge in the web panel).
    service = KnowledgeService.from_session_factory(
        session_factory,
        DeterministicEmbeddingClient(dimension=256),
        _degraded_reranker(),
    )
    app = create_app()
    authenticate_app(app)
    app.dependency_overrides[get_knowledge_service] = lambda: service
    app.dependency_overrides[get_knowledge_session_factory] = lambda: session_factory

    with TestClient(app) as client:
        indexed = client.post("/knowledge/index", json=_index_payload(source_id))
        assert indexed.status_code == 200, indexed.text

        resp = client.post(
            "/knowledge/search",
            json={
                "query": "validate an oauth jwt token",
                "scope": {"workspace_id": WORKSPACE_ID},
                "k": 3,
            },
        )

    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert results, "a degraded reranker must still return weighted-RRF results"
    assert all(c["rerank_score"] is None for c in results)
    # The envelope names what this call did: the configured provider's call
    # degraded to weighted-RRF, so it reports "fallback", not "jina". The
    # server-side fallback telemetry agrees.
    assert resp.json()["rerank"] == "fallback"
    assert service.last_rerank is not None
    assert service.last_rerank.fallback_used is True


# --------------------------------------------------------------------------- #
# J retrieval: honest BM25-only search + strict-vector 503 (ADR-0018)          #
# --------------------------------------------------------------------------- #


@pytest.fixture
def bm25_only_client(
    session_factory: sessionmaker[Session],
    authenticate_app: Callable[..., FastAPI],
) -> Iterator[TestClient]:
    # The production default shape: no embedder, no reranker configured.
    service = KnowledgeService.from_session_factory(session_factory, None, None)
    app = create_app()
    authenticate_app(app)
    app.dependency_overrides[get_knowledge_service] = lambda: service
    app.dependency_overrides[get_knowledge_session_factory] = lambda: session_factory
    with TestClient(app) as c:
        yield c


def test_bm25_only_search_reports_unconfigured_vector_leg(
    bm25_only_client: TestClient, source_id: uuid.UUID
) -> None:
    indexed = bm25_only_client.post("/knowledge/index", json=_index_payload(source_id))
    assert indexed.status_code == 200, indexed.text
    assert indexed.json()["indexed"] == 3

    resp = bm25_only_client.post(
        "/knowledge/search",
        json={
            "query": "validate an oauth jwt token",
            "scope": {"workspace_id": WORKSPACE_ID},
            "k": 3,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["legs"] == ["bm25"]
    assert body["vector_leg"] == "unconfigured"
    assert body["rerank"] == "off"
    assert body["results"], "BM25-only mode must still answer keyword queries"
    assert body["results"][0]["path"] == "auth.py"
    assert all(c["rerank_score"] is None for c in body["results"])


def test_require_vector_returns_503_when_unconfigured(
    bm25_only_client: TestClient, source_id: uuid.UUID
) -> None:
    bm25_only_client.post("/knowledge/index", json=_index_payload(source_id))
    resp = bm25_only_client.post(
        "/knowledge/search",
        json={
            "query": "validate an oauth jwt token",
            "scope": {"workspace_id": WORKSPACE_ID},
            "k": 3,
            "require_vector": True,
        },
    )
    assert resp.status_code == 503, resp.text
    assert "unconfigured" in resp.json()["detail"]


def test_require_vector_succeeds_when_configured(client: TestClient, source_id: uuid.UUID) -> None:
    client.post("/knowledge/index", json=_index_payload(source_id))
    resp = client.post(
        "/knowledge/search",
        json={
            "query": "validate an oauth jwt token",
            "scope": {"workspace_id": WORKSPACE_ID},
            "k": 3,
            "require_vector": True,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["vector_leg"] == "configured"


# --------------------------------------------------------------------------- #
# Tracker sources and commit history (spec J §2.3)                             #
# --------------------------------------------------------------------------- #


def _add_source(
    session_factory: sessionmaker[Session], workspace_id: str, **columns: object
) -> uuid.UUID:
    with session_factory() as session:
        source = KnowledgeSource(workspace_id=uuid.UUID(workspace_id), name="s", **columns)
        session.add(source)
        session.commit()
        return source.id


def test_sync_of_a_tracker_source_enqueues_the_worker_sync(
    client: TestClient,
    session_factory: sessionmaker[Session],
    source_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from forge_api.routers import knowledge as knowledge_router

    enqueued: list[str] = []
    monkeypatch.setattr(knowledge_router, "enqueue_source_sync", enqueued.append)
    tracker = _add_source(
        session_factory,
        WORKSPACE_ID,
        kind="url",
        uri="https://github.com/org/api",
        config={"tracker": "github"},
    )

    resp = client.post("/knowledge/sync", json={"source_id": str(tracker), "mode": "full"})

    assert resp.status_code == 202, resp.text
    assert resp.json() == {"status": "enqueued", "source_id": str(tracker)}
    assert enqueued == [str(tracker)]
    assert knowledge_router.SYNC_SOURCE_TASK == "forge.knowledge.sync_source"

    # Another tenant's tracker is never enqueued.
    other_ws, _ = _seed_other_workspace(session_factory)
    foreign = _add_source(
        session_factory,
        str(other_ws),
        kind="url",
        uri="https://github.com/other/secret",
        config={"tracker": "github"},
    )
    assert client.post("/knowledge/sync", json={"source_id": str(foreign)}).status_code == 404
    assert enqueued == [str(tracker)]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def test_sync_of_a_repo_source_indexes_commits_only_when_its_config_enables_them(
    client: TestClient, session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "forge@example.com")
    _git(repo, "config", "user.name", "Forge Test")
    _git(repo, "config", "commit.gpgsign", "false")
    for i in range(2):
        (repo / "a.py").write_text(f"a = {i}\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", f"Commit {i}")

    plain = _add_source(session_factory, WORKSPACE_ID, kind="repo", uri="github.com/org/plain")
    enabled = _add_source(
        session_factory,
        WORKSPACE_ID,
        kind="repo",
        uri="github.com/org/history",
        config={"commits": {"enabled": True, "max_count": 1}},
    )
    for source in (plain, enabled):
        resp = client.post("/knowledge/sync", json={"source_id": str(source), "root": str(repo)})
        assert resp.status_code == 200, resp.text

    def commit_paths(source: uuid.UUID) -> list[str]:
        with session_factory() as session:
            paths = session.scalars(
                select(RetrievalChunk.path).where(RetrievalChunk.knowledge_source_id == source)
            )
            return sorted(p for p in paths if p and p.startswith("commits/"))

    assert commit_paths(plain) == []
    assert len(commit_paths(enabled)) == 1
