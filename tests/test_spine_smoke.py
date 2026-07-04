"""End-to-end Knowledge/RAG spine smoke (plan Task 2.4, Phase 2).

This is the proof the retrieval spine works as one piece. It drives the *real*
pipeline end to end against a real on-disk source tree (``examples/sample-repo``):

    read files from disk
      -> chunk (AST for .py, paragraph for .md)
      -> full_sync index  (embed + persist; BM25 tsvector on Postgres)
      -> hybrid search: semantic (pgvector cosine) + keyword (BM25)
                        -> RRF fusion (k = 60)
                        -> cross-encoder rerank (+ chunk-type weight boost)
      -> attributed top-k (path / source_id / source_uri / line span)

and then prints the golden-retrieval eval numbers (recall@k / MRR) produced by
``packages/evaluation`` over the genuine :class:`~forge_knowledge.KnowledgeService`.

Provider posture (plan Global Constraints: "no real external API calls"):
the *pipeline and metrics are real*; only the learned models and the database
backend are stood in for offline. The active path uses the deterministic
embedding client (signed feature hashing), the fixture reranker (token overlap),
and an in-memory SQLite backend whose dialect-aware stores compute cosine / BM25
in Python.

# PARKED: the live path is not exercised here.
#   * Embeddings: ``forge_knowledge.HttpEmbeddingClient`` (OpenAI-compatible BYOK)
#     and reranker ``forge_knowledge.JinaRerankerClient`` need a network endpoint
#     + API key -> forbidden overnight (no real external calls).
#   * Postgres pgvector ``cosine_distance`` + ``ts_rank`` BM25 need a live
#     pgvector database; none is configured (no FORGE_TEST_DATABASE_URL, the
#     ``testcontainers`` extra is not installed). The same stores run unchanged
#     against Postgres in Phase 2 / CI; see ``test_stores_postgres.py``.
# Both swap in behind the frozen contracts without touching this driver.

Run it directly to print the full smoke report:

    uv run python tests/test_spine_smoke.py
    # or, with pytest capturing disabled:
    uv run pytest tests/test_spine_smoke.py -s
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine

from forge_contracts.constants import RRF_K
from forge_contracts.dtos import KnowledgeScope, RetrievedChunk
from forge_db.base import Base
from forge_db.models import KnowledgeSource, Workspace
from forge_db.session import create_session_factory
from forge_eval.report import format_scorecard
from forge_eval.retrieval_eval import (
    DEFAULT_K,
    DEFAULT_RECALL_THRESHOLD,
    run_retrieval_eval,
)
from forge_eval.runner import Scorecard
from forge_knowledge import (
    DeterministicEmbeddingClient,
    FixtureRerankerClient,
    KnowledgeService,
    full_sync,
    read_repo_files,
)

#: The on-disk sample repository indexed by the smoke (a few .py + .md files).
SAMPLE_REPO_DIR = Path(__file__).resolve().parent.parent / "examples" / "sample-repo"

#: Attribution carried through the pipeline for this source.
SAMPLE_SOURCE_URI = "github.com/forge/pulse"

#: Embedding width for the offline deterministic client.
EMBEDDING_DIM = 512

#: How many attributed results the smoke surfaces per query.
SEARCH_K = 5

#: Golden-retrieval eval gate for the smoke (mean reciprocal rank floor).
SMOKE_MRR_THRESHOLD = 0.80

#: Smoke queries -> the file each must surface. They deliberately mix
#: natural-language phrasing (exercises the semantic leg) with an exact
#: identifier (exercises the BM25 keyword leg) so a single leg cannot pass alone.
SMOKE_QUERIES: list[tuple[str, str, str]] = [
    ("pooled postgres database connection", "pulse/db.py", "semantic"),
    ("verify an oauth jwt bearer token signature and expiry", "pulse/auth.py", "semantic"),
    ("verify_token", "pulse/auth.py", "identifier"),
    ("persist a brand new task record into the database", "pulse/tasks.py", "semantic"),
    ("post a slack notification message to a channel", "pulse/notifications.py", "semantic"),
    ("reciprocal rank fusion combine semantic and keyword rankings", "pulse/search.py", "semantic"),
    ("what is pulse incident and task orchestration service", "README.md", "doc"),
]


# --------------------------------------------------------------------------- #
# Pipeline driver (the real spine, offline backends)                          #
# --------------------------------------------------------------------------- #


def build_service_from_directory(
    root: Path,
    *,
    dimension: int = EMBEDDING_DIM,
) -> tuple[KnowledgeService, KnowledgeScope, str]:
    """Index a real on-disk source tree through the genuine pipeline.

    Reads every indexable file from ``root``, chunks + embeds + persists them via
    :func:`forge_knowledge.full_sync` (the real ingestion path), and returns a
    ready :class:`KnowledgeService`, the scope to search it under, and the indexed
    source id.
    """
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)

    with factory() as session:
        workspace = Workspace(name="Pulse", slug="pulse")
        session.add(workspace)
        session.flush()
        workspace_id = workspace.id
        source = KnowledgeSource(
            workspace_id=workspace_id,
            kind="repo",
            name="pulse",
            uri=SAMPLE_SOURCE_URI,
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
    files = read_repo_files(root)
    full_sync(service, str(source_id), files)
    return service, KnowledgeScope(workspace_id=workspace_id), str(source_id)


def _rank_of(results: list[RetrievedChunk], expected_path: str) -> int | None:
    """1-based rank of the first result whose path is ``expected_path``."""
    for position, hit in enumerate(results, start=1):
        if hit.path == expected_path:
            return position
    return None


def format_results(query: str, results: list[RetrievedChunk]) -> str:
    """Render attributed, reranked results for a query (console friendly)."""
    lines = [f"query: {query!r}"]
    if not results:
        lines.append("  (no results)")
        return "\n".join(lines)
    for position, hit in enumerate(results, start=1):
        span = (
            f":{hit.start_line}-{hit.end_line}"
            if hit.start_line is not None and hit.end_line is not None
            else ""
        )
        rerank = "-" if hit.rerank_score is None else f"{hit.rerank_score:.3f}"
        lines.append(
            f"  {position}. {hit.path}{span}  "
            f"score={hit.score:.3f} rerank={rerank} weight={hit.weight:.2f} "
            f"[{hit.chunk_type.value}] <- {hit.source_uri}"
        )
    return "\n".join(lines)


def run_spine_smoke() -> tuple[str, Scorecard]:
    """Drive the full spine and return (human-readable report, eval scorecard)."""
    service, scope, _source_id = build_service_from_directory(SAMPLE_REPO_DIR)

    blocks: list[str] = []
    blocks.append("=" * 72)
    blocks.append("Forge Knowledge/RAG spine smoke (Task 2.4)")
    blocks.append(f"indexed: {SAMPLE_REPO_DIR}  (source: {SAMPLE_SOURCE_URI})")
    blocks.append(f"pipeline: semantic + BM25 -> RRF (k={RRF_K}) -> rerank -> top-{SEARCH_K}")
    blocks.append("backends: DeterministicEmbeddingClient + FixtureRerankerClient + SQLite")
    blocks.append("=" * 72)
    blocks.append("")
    blocks.append("ATTRIBUTED, RERANKED RESULTS")
    blocks.append("-" * 72)
    for query, _expected, _tag in SMOKE_QUERIES:
        results = service.search(query, scope, k=SEARCH_K)
        blocks.append(format_results(query, results))
        blocks.append("")

    card = run_retrieval_eval(k=DEFAULT_K, recall_threshold=DEFAULT_RECALL_THRESHOLD)
    blocks.append("GOLDEN RETRIEVAL EVAL (packages/evaluation)")
    blocks.append("-" * 72)
    blocks.append(format_scorecard(card))
    blocks.append("")
    blocks.append(
        f"[RAG spine] sample-repo queries={len(SMOKE_QUERIES)}  "
        f"golden recall@{card.k}={card.mean_recall_at_k:.3f}  "
        f"MRR={card.mean_mrr:.3f}  hit_rate={card.hit_rate:.3f}"
    )
    return "\n".join(blocks), card


# --------------------------------------------------------------------------- #
# Tests (green gate)                                                           #
# --------------------------------------------------------------------------- #


def test_sample_repo_has_python_and_markdown_files() -> None:
    assert SAMPLE_REPO_DIR.is_dir(), f"missing sample repo: {SAMPLE_REPO_DIR}"
    files = read_repo_files(SAMPLE_REPO_DIR)
    assert any(p.endswith(".py") for p in files), "sample repo has no .py files"
    assert any(p.endswith(".md") for p in files), "sample repo has no .md files"


def test_full_sync_indexes_the_on_disk_repo() -> None:
    service, _scope, source_id = build_service_from_directory(SAMPLE_REPO_DIR)
    indexed_paths = service.source_paths(source_id)
    files = set(read_repo_files(SAMPLE_REPO_DIR))
    # Every file on disk is represented by at least one indexed chunk.
    assert indexed_paths == files


def test_hybrid_search_returns_attributed_reranked_results() -> None:
    service, scope, _source_id = build_service_from_directory(SAMPLE_REPO_DIR)
    results = service.search("pooled postgres database connection", scope, k=SEARCH_K)

    assert results, "hybrid search returned nothing"
    for hit in results:
        # Source attribution survives the whole pipeline.
        assert hit.path, "result missing path attribution"
        assert hit.source_id, "result missing source_id attribution"
        assert hit.source_uri == SAMPLE_SOURCE_URI, "result missing source_uri"
        assert hit.start_line is not None and hit.end_line is not None
        # The reranker actually ran (cross-encoder score recorded on every hit).
        assert hit.rerank_score is not None, "result was not reranked"
    # Results are ordered by descending final (weight-boosted) score.
    scores = [hit.score for hit in results]
    assert scores == sorted(scores, reverse=True)


def test_rrf_fusion_constant_is_sixty() -> None:
    # The fusion stage is RRF with k = 60 (plan Global Constraints).
    assert RRF_K == 60


def test_smoke_queries_retrieve_expected_files() -> None:
    service, scope, _source_id = build_service_from_directory(SAMPLE_REPO_DIR)
    misses: list[str] = []
    for query, expected, _tag in SMOKE_QUERIES:
        results = service.search(query, scope, k=SEARCH_K)
        rank = _rank_of(results, expected)
        if rank is None:
            top = [hit.path for hit in results]
            misses.append(f"{query!r} expected {expected} in top-{SEARCH_K}, got {top}")
    assert not misses, "smoke retrieval misses:\n" + "\n".join(misses)


def test_identifier_query_is_recovered_at_rank_one() -> None:
    """The BM25 keyword leg must surface an exact identifier first."""
    service, scope, _source_id = build_service_from_directory(SAMPLE_REPO_DIR)
    results = service.search("verify_token", scope, k=SEARCH_K)
    assert results, "identifier query returned nothing"
    assert results[0].path == "pulse/auth.py", (
        f"expected pulse/auth.py at rank 1, got {[h.path for h in results[:3]]}"
    )


def test_golden_retrieval_eval_meets_recall_and_mrr_threshold(
    capsys: pytest.CaptureFixture[str],
) -> None:
    report, card = run_spine_smoke()

    # Surface the full smoke report + eval numbers for the morning report.
    with capsys.disabled():
        print("\n" + report)

    assert card.num_cases >= 15
    assert card.mean_recall_at_k >= DEFAULT_RECALL_THRESHOLD
    assert card.mean_mrr >= SMOKE_MRR_THRESHOLD
    assert card.passed
    card.assert_threshold()


def main() -> int:
    """Print the full spine smoke report; return a process exit code."""
    report, card = run_spine_smoke()
    print(report)
    return 0 if card.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
