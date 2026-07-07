"""Spine retrieval eval: recall@k and MRR over the real pipeline (Task 1.3).

This is a compact, hermetic quality check for the hybrid-retrieval spine — the
full golden retrieval set + RAGAS-style report is Task 1.4's deliverable in
``forge_eval``. Here a small synthetic repo (code + docs) is indexed through the
real :class:`KnowledgeService` (deterministic embedding + fixture reranker), and
a set of >=15 ``query -> expected-path`` pairs is scored with:

* **recall@k**: fraction of queries whose expected chunk appears in the top-k;
* **MRR**: mean reciprocal rank of the expected chunk.

The thresholds are conservative (the offline deterministic pipeline is weaker
than a learned model) but prove the semantic + keyword + RRF + rerank chain
genuinely retrieves the right chunk, not noise. The numbers are printed for the
morning report.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from forge_contracts.dtos import Chunk, KnowledgeScope
from forge_db.base import Base
from forge_db.models import KnowledgeSource, Workspace
from forge_db.session import create_session_factory
from forge_knowledge import (
    DeterministicEmbeddingClient,
    FixtureRerankerClient,
    KnowledgeService,
)

# A small synthetic repository: each (path, content) is a distinct, identifiable
# unit. Kept as tuples so the chunk bodies stay within the line-length limit.
_DOCS: list[tuple[str, str]] = [
    ("db/pool.py", "def connect_postgres(): open a pooled database connection with psycopg"),
    ("db/migrate.py", "def run_migration(): apply alembic database schema migrations upgrade head"),
    ("auth/jwt.py", "def validate_jwt(token): verify oauth2 jwt signature audience and expiry"),
    ("auth/password.py", "def hash_password(pw): bcrypt salted password hashing and verification"),
    ("ui/dashboard.py", "def render_dashboard(): react component charts with useState hooks"),
    ("vcs/github.py", "def open_pull_request(): github api creates a pull request reviewers"),
    ("notify/slack.py", "def send_slack_message(): post a slack notification to a channel"),
    ("infra/k8s.py", "def schedule_pod(): kubernetes pod scheduling autoscaling node affinity"),
    ("search/rrf.py", "def compute_rrf_score(rankings): reciprocal rank fusion k equals sixty"),
    ("search/rerank.py", "def rerank_results(): cross encoder reranker reorders candidates"),
    ("search/embed.py", "def embed_text(): generate dense vector embeddings for semantic search"),
    ("vcs/webhook.py", "def parse_webhook(): parse github ci check run status webhook payload"),
]
CORPUS: list[Chunk] = [Chunk(content=body, path=path) for path, body in _DOCS]

# >=15 query -> expected chunk path pairs.
GOLDEN: list[tuple[str, str]] = [
    ("pooled postgres database connection", "db/pool.py"),
    ("apply alembic schema migrations", "db/migrate.py"),
    ("verify an oauth jwt token signature", "auth/jwt.py"),
    ("validate_jwt", "auth/jwt.py"),
    ("bcrypt password hashing", "auth/password.py"),
    ("react dashboard charts useState", "ui/dashboard.py"),
    ("create a github pull request with reviewers", "vcs/github.py"),
    ("send a slack channel notification", "notify/slack.py"),
    ("kubernetes pod autoscaling node affinity", "infra/k8s.py"),
    ("reciprocal rank fusion score", "search/rrf.py"),
    ("compute_rrf_score", "search/rrf.py"),
    ("cross encoder reranker reorder documents", "search/rerank.py"),
    ("dense vector embeddings for semantic search", "search/embed.py"),
    ("parse github ci check run webhook", "vcs/webhook.py"),
    ("hash and verify a salted password", "auth/password.py"),
    ("open pull request github reviewers", "vcs/github.py"),
]


@pytest.fixture
def indexed_service() -> tuple[KnowledgeService, KnowledgeScope]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        workspace = Workspace(name="Acme", slug="acme")
        session.add(workspace)
        session.flush()
        ws_id = workspace.id
        source = KnowledgeSource(
            workspace_id=ws_id, kind="repo", name="app", uri="github.com/org/app"
        )
        session.add(source)
        session.flush()
        source_id = source.id
        session.commit()

    service = KnowledgeService.from_session_factory(
        factory, DeterministicEmbeddingClient(dimension=512), FixtureRerankerClient()
    )
    service.index(str(source_id), CORPUS)
    return service, KnowledgeScope(workspace_id=ws_id)


def _rank_of(results: list[str], expected: str) -> int | None:
    for position, path in enumerate(results, start=1):
        if path == expected:
            return position
    return None


def test_recall_at_k_and_mrr_meet_threshold(
    indexed_service: tuple[KnowledgeService, KnowledgeScope],
) -> None:
    service, scope = indexed_service
    k = 3

    hits_at_k = 0
    reciprocal_ranks: list[float] = []
    for query, expected in GOLDEN:
        results = [c.path or "" for c in service.search(query, scope, k=k)]
        rank = _rank_of(results, expected)
        if rank is not None:
            hits_at_k += 1
        reciprocal_ranks.append(1.0 / rank if rank is not None else 0.0)

    recall_at_k = hits_at_k / len(GOLDEN)
    mrr = sum(reciprocal_ranks) / len(GOLDEN)

    # Surface the numbers for the morning report.
    print(f"\n[RAG spine eval] queries={len(GOLDEN)} recall@{k}={recall_at_k:.3f} MRR={mrr:.3f}")

    assert recall_at_k >= 0.8, f"recall@{k}={recall_at_k:.3f} below threshold"
    assert mrr >= 0.7, f"MRR={mrr:.3f} below threshold"
