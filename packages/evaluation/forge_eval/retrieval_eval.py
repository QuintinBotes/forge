"""Golden retrieval eval over the *real* hybrid pipeline (plan Task 1.4, spine).

This is the spine's quality proof in ``forge_eval``: it indexes a fixed sample
repository through the genuine :class:`~forge_knowledge.KnowledgeService`
(semantic pgvector leg + BM25 keyword leg -> RRF fusion -> cross-encoder rerank)
and scores the golden retrieval set (``data/golden_retrieval.json``) with
:mod:`forge_eval.runner`, producing recall@k / MRR and a regression gate.

It uses the offline, deterministic clients (hashing embedding + token-overlap
reranker) so it runs with **no network and no model provider** — the pipeline and
the metrics are real; only the learned models are stood in for. A production run
swaps in a BYOK embedding client + Jina reranker behind the same interfaces.

This module is imported on demand (it pulls in ``forge_knowledge`` /
``forge_db``); the base :mod:`forge_eval` package stays dependency-light.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import create_engine

from forge_contracts.dtos import Chunk, KnowledgeScope
from forge_db.base import Base
from forge_db.models import KnowledgeSource, Workspace
from forge_db.session import create_session_factory
from forge_eval.golden import GoldenCase, load_golden_set
from forge_eval.runner import RetrieveFn, Scorecard, evaluate_retrieval
from forge_knowledge import (
    DeterministicEmbeddingClient,
    FixtureRerankerClient,
    KnowledgeService,
    chunk_file,
)

__all__ = [
    "DEFAULT_EMBEDDING_DIM",
    "DEFAULT_K",
    "DEFAULT_RECALL_THRESHOLD",
    "DEFAULT_SEARCH_K",
    "GOLDEN_RETRIEVAL_PATH",
    "SAMPLE_CORPUS",
    "build_indexed_service",
    "load_golden_retrieval",
    "make_retrieve_fn",
    "run_retrieval_eval",
]

#: Path to the on-disk golden retrieval set (the source of truth for queries).
GOLDEN_RETRIEVAL_PATH = Path(__file__).resolve().parent / "data" / "golden_retrieval.json"

#: The fixed sample repository the golden set is graded against. Each value is a
#: single, identifiable unit so its file ``path`` is the ground-truth chunk id.
#: Topics span code (semantic + exact-identifier queries) and one doc file.
SAMPLE_CORPUS: dict[str, str] = {
    "db/pool.py": (
        "def connect_postgres():\n"
        "    # open a pooled database connection with psycopg and sqlalchemy\n"
        "    return create_pool(dsn)\n"
    ),
    "db/migrate.py": (
        "def run_migration():\n"
        "    # apply alembic database schema migrations to upgrade head\n"
        "    return alembic_upgrade('head')\n"
    ),
    "auth/jwt.py": (
        "def validate_jwt(token):\n"
        "    # verify an oauth2 jwt signature audience and expiry claims\n"
        "    return decode_and_verify(token)\n"
    ),
    "auth/password.py": (
        "def hash_password(pw):\n"
        "    # bcrypt salted password hashing and constant time verification\n"
        "    return bcrypt_hash(pw)\n"
    ),
    "auth/rbac.py": (
        "def check_permission(user, action):\n"
        "    # role based access control admin member viewer agent_runner\n"
        "    return user.role.allows(action)\n"
    ),
    "ui/dashboard.py": (
        "def render_dashboard():\n"
        "    # react component renders charts with useState and useEffect hooks\n"
        "    return Dashboard()\n"
    ),
    "ui/table.py": (
        "def render_table():\n"
        "    # tanstack table sortable filterable paginated data grid component\n"
        "    return DataTable()\n"
    ),
    "vcs/github.py": (
        "def open_pull_request():\n"
        "    # github api creates a pull request and requests reviewers\n"
        "    return gh.create_pr()\n"
    ),
    "vcs/webhook.py": (
        "def parse_webhook():\n"
        "    # parse a github ci check run status webhook payload signature\n"
        "    return parse_check_run(payload)\n"
    ),
    "notify/slack.py": (
        "def send_slack_message():\n"
        "    # post a slack notification message to a channel via webhook\n"
        "    return slack.post(channel, text)\n"
    ),
    "infra/k8s.py": (
        "def schedule_pod():\n"
        "    # kubernetes pod scheduling with autoscaling and node affinity rules\n"
        "    return k8s.schedule(pod)\n"
    ),
    "infra/cache.py": (
        "def get_redis_client():\n"
        "    # connect to redis for caching and the celery task queue broker\n"
        "    return redis.Redis()\n"
    ),
    "search/rrf.py": (
        "def compute_rrf_score(rankings):\n"
        "    # reciprocal rank fusion combine rankings with k equals sixty\n"
        "    return sum(1.0 / (60 + r) for r in rankings)\n"
    ),
    "search/rerank.py": (
        "def rerank_results(query, docs):\n"
        "    # cross encoder reranker reorders candidate documents by relevance\n"
        "    return reranker.rerank(query, docs)\n"
    ),
    "search/embed.py": (
        "def embed_text(text):\n"
        "    # generate dense vector embeddings for semantic similarity search\n"
        "    return model.embed(text)\n"
    ),
    "search/bm25.py": (
        "def bm25_score():\n"
        "    # okapi bm25 keyword lexical ranking with tsvector full text search\n"
        "    return ts_rank(tsv, query)\n"
    ),
    "policy/evaluator.py": (
        "def evaluate_policy(action):\n"
        "    # allow or deny a tool call against write rules and path globs\n"
        "    return decision(action)\n"
    ),
    "spec/manifest.py": (
        "def write_manifest():\n"
        "    # serialize the spec manifest yaml with requirements and acceptance\n"
        "    return yaml.dump(manifest)\n"
    ),
    "agent/loop.py": (
        "def run_agent(objective):\n"
        "    # langgraph state graph plan act observe single agent loop\n"
        "    return graph.invoke(objective)\n"
    ),
    "docs/README.md": (
        "# Forge\n\n"
        "Forge is an open source engineering orchestration platform for AI agents.\n"
    ),
}

#: k for recall@k / hit@k in the headline eval.
DEFAULT_K = 5

#: How many candidates the pipeline returns per query before metric windowing.
DEFAULT_SEARCH_K = 10

#: Embedding dimensionality for the deterministic client (offline default).
DEFAULT_EMBEDDING_DIM = 512

#: Conservative regression gate for the offline deterministic pipeline. The real
#: measured number is higher (printed by the report); this is the floor below
#: which a regression must fail CI.
DEFAULT_RECALL_THRESHOLD = 0.85


def load_golden_retrieval() -> list[GoldenCase]:
    """Load the golden retrieval set from disk."""
    return load_golden_set(GOLDEN_RETRIEVAL_PATH)


def build_indexed_service(
    corpus: dict[str, str] | None = None,
    *,
    dimension: int = DEFAULT_EMBEDDING_DIM,
) -> tuple[KnowledgeService, KnowledgeScope]:
    """Index ``corpus`` through the real pipeline; return service + search scope.

    Uses in-memory SQLite (the dialect-aware stores compute cosine / BM25 in
    Python there), so this is fully hermetic.
    """
    documents = SAMPLE_CORPUS if corpus is None else corpus
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)

    with factory() as session:
        workspace = Workspace(name="Eval", slug="eval")
        session.add(workspace)
        session.flush()
        workspace_id = workspace.id
        source = KnowledgeSource(
            workspace_id=workspace_id,
            kind="repo",
            name="sample",
            uri="github.com/forge/sample",
        )
        session.add(source)
        session.flush()
        source_id = source.id
        session.commit()

    service = KnowledgeService.from_session_factory(
        factory,
        DeterministicEmbeddingClient(dimension=dimension),
        FixtureRerankerClient(),
    )
    chunks: list[Chunk] = []
    for path, src in documents.items():
        chunks.extend(chunk_file(path, src))
    service.index(str(source_id), chunks)
    return service, KnowledgeScope(workspace_id=workspace_id)


def make_retrieve_fn(
    service: KnowledgeService,
    scope: KnowledgeScope,
    *,
    search_k: int = DEFAULT_SEARCH_K,
) -> RetrieveFn:
    """Adapt the knowledge service to a :data:`RetrieveFn` over chunk *paths*.

    Returns each query's results as an ordered, de-duplicated list of file paths
    (the golden set's ground-truth ids), so a multi-chunk file counts once.
    """

    def retrieve(case: GoldenCase) -> Sequence[str]:
        ordered: list[str] = []
        for hit in service.search(case.query, scope, k=search_k):
            path = hit.path or ""
            if path and path not in ordered:
                ordered.append(path)
        return ordered

    return retrieve


def run_retrieval_eval(
    *,
    k: int = DEFAULT_K,
    recall_threshold: float = DEFAULT_RECALL_THRESHOLD,
    search_k: int = DEFAULT_SEARCH_K,
) -> Scorecard:
    """Index the sample corpus and score the golden retrieval set end-to-end."""
    cases = load_golden_retrieval()
    service, scope = build_indexed_service()
    retrieve_fn = make_retrieve_fn(service, scope, search_k=search_k)
    return evaluate_retrieval(cases, retrieve_fn, k=k, recall_threshold=recall_threshold)
