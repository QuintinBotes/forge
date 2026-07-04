"""Honest real-corpus retrieval eval tests (HARD-04).

Two tiers:

* **Hermetic** (default suite, no model, no network): golden-set validity, the
  red-flag guard, secret exclusion + redaction on a seeded corpus, and tenant
  isolation — all proven with the deterministic embedder, since redaction and the
  SQL scope filter are embedder-independent.
* **``realeval``** (opt-in via ``FORGE_RUN_REALEVAL=1`` + the ``eval`` extra;
  ``pytest.importorskip('sentence_transformers')``): the four honest metrics, the
  not-perfect guard on the *measured* run, the ablation, the regression gate, the
  no-call-time-network assertion, and the learned-embedder isolation/redaction
  re-proofs. These load the local ``sentence-transformers`` model from cache.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine

from forge_contracts.dtos import KnowledgeScope
from forge_db.base import Base
from forge_db.models import KnowledgeSource, Workspace
from forge_db.session import create_session_factory
from forge_eval.corpus_eval import (
    DEFAULT_NDCG_FLOOR,
    DEFAULT_RECALL_FLOOR,
    GOLDEN_REAL_PATH,
    build_eval_context,
    format_eval_report,
    is_suspiciously_perfect,
    mean_recall_at,
    resolve_embedder,
    resolve_reranker,
    run_ablation,
    run_real_retrieval_eval,
)
from forge_eval.golden import load_golden_set
from forge_eval.real_corpus import (
    build_real_indexed_service,
    build_repo_corpus,
    repo_root,
)
from forge_eval.runner import CaseResult, Scorecard
from forge_knowledge import DeterministicEmbeddingClient, FixtureRerankerClient
from forge_knowledge.redaction import REDACTED

MIN_REAL_CASES = 30
_FAKE_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
_FAKE_PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX6Ppy1tPf9Cnzj4p4WGeKLs1Pt8Q\n"
    "-----END RSA PRIVATE KEY-----"
)

RUN_REALEVAL = os.environ.get("FORGE_RUN_REALEVAL") == "1"
requires_realeval = pytest.mark.skipif(
    not RUN_REALEVAL,
    reason="set FORGE_RUN_REALEVAL=1 (and install the 'eval' extra) to run the "
    "learned-model real-corpus eval",
)


# --------------------------------------------------------------------------- #
# Hermetic — golden validity, red flag, secrets, isolation                     #
# --------------------------------------------------------------------------- #


def test_real_golden_set_valid() -> None:
    """AC7: >=30 cases; every expected path is in the real corpus; ids unique; tags mixed."""
    corpus = set(build_repo_corpus(repo_root()))
    cases = load_golden_set(GOLDEN_REAL_PATH)
    assert len(cases) >= MIN_REAL_CASES
    assert len({c.id for c in cases}) == len(cases)
    for case in cases:
        assert case.expected_ids, f"{case.id} has no expected_ids"
        for path in case.expected_ids:
            assert path in corpus, f"{case.id} references path not in corpus: {path!r}"
    tags = {t for c in cases for t in c.tags}
    # Semantic, exact-identifier, and cross-file kinds are all represented.
    assert {"semantic", "identifier", "cross-file"} <= tags
    assert sum("identifier" in c.tags for c in cases) >= 5
    assert sum("semantic" in c.tags for c in cases) >= 10


def test_red_flag_guard_flags_synthetic_perfect() -> None:
    """AC3: a perfect-1.000 scorecard is flagged as suspected leakage, not passed."""
    perfect = Scorecard(
        k=5,
        recall_threshold=0.0,
        results=[
            CaseResult(f"c{i}", ["p"], 1.0, 1.0, 1.0, hit=True, passed=True, ndcg_at_k=1.0)
            for i in range(10)
        ],
    )
    assert is_suspiciously_perfect(perfect) is True

    realistic = Scorecard(
        k=5,
        recall_threshold=0.0,
        results=[
            CaseResult(f"c{i}", ["p"], r, r, r, hit=r > 0, passed=True, ndcg_at_k=r)
            for i, r in enumerate([1.0, 0.0, 1.0, 1.0, 0.0])
        ],
    )
    assert is_suspiciously_perfect(realistic) is False


def test_build_repo_corpus_excludes_and_redacts_secrets(tmp_path) -> None:
    """AC14: secret files are excluded; any secret in text is redacted."""
    (tmp_path / "packages" / "x").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / ".env").write_text(f"OPENAI_API_KEY={_FAKE_AWS_KEY}\n", encoding="utf-8")
    (tmp_path / "deploy" / "secrets").mkdir(parents=True)
    (tmp_path / "deploy" / "secrets" / "app.pem").write_text(_FAKE_PEM, encoding="utf-8")
    (tmp_path / "packages" / "x" / "leaky.py").write_text(
        f"AWS = '{_FAKE_AWS_KEY}'\n" + _FAKE_PEM + "\n", encoding="utf-8"
    )
    (tmp_path / "docs" / "ok.md").write_text("# clean doc\n", encoding="utf-8")

    corpus = build_repo_corpus(tmp_path)
    # Secret-bearing files never enter the corpus.
    assert not any(p.endswith(".env") for p in corpus)
    assert not any(p.endswith(".pem") for p in corpus)
    assert "deploy/secrets/app.pem" not in corpus
    # A source file that *contains* a secret is admitted but redacted.
    assert "packages/x/leaky.py" in corpus
    body = corpus["packages/x/leaky.py"]
    assert _FAKE_AWS_KEY not in body
    assert "PRIVATE KEY" not in body
    assert REDACTED in body


def test_redaction_holds_before_persistence_hermetic() -> None:
    """AC14: a seeded secret is redacted in the stored chunk and never in results."""
    corpus = {
        "src/config.py": f"API_TOKEN = '{_FAKE_AWS_KEY}'\ndef load(): return API_TOKEN\n",
        "src/keys.py": _FAKE_PEM + "\n\ndef signer(): ...\n",
    }
    service, scope = build_real_indexed_service(
        corpus, DeterministicEmbeddingClient(dimension=64), FixtureRerankerClient()
    )
    hits = service.search("api token config loader signer", scope, k=10)
    assert hits, "expected some results"
    for hit in hits:
        assert _FAKE_AWS_KEY not in hit.content
        assert "PRIVATE KEY" not in hit.content


def _two_tenant_service(embedder):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    ids: dict[str, tuple[str, str]] = {}
    for slug in ("a", "b"):
        with factory() as session:
            ws = Workspace(name=slug.upper(), slug=slug)
            session.add(ws)
            session.flush()
            src = KnowledgeSource(
                workspace_id=ws.id, kind="repo", name=f"repo-{slug}", uri=f"x/{slug}"
            )
            session.add(src)
            session.flush()
            ids[slug] = (str(ws.id), str(src.id))
            session.commit()
    from forge_knowledge import KnowledgeService, chunk_file

    service = KnowledgeService.from_session_factory(factory, embedder, FixtureRerankerClient())
    shared = (
        "def authenticate(user):\n"
        "    # identical content indexed under both tenants\n"
        "    return True\n"
    )
    for slug in ("a", "b"):
        service.index(ids[slug][1], chunk_file("auth.py", shared))
    return service, ids


def test_tenant_isolation_hermetic() -> None:
    """AC8: identical content under two workspaces never crosses scope (row-id proof)."""
    service, ids = _two_tenant_service(DeterministicEmbeddingClient(dimension=64))
    ws_a, _ = ids["a"]
    ws_b, _ = ids["b"]
    hits_a = service.search("authenticate user", KnowledgeScope(workspace_id=ws_a), k=10)
    hits_b = service.search("authenticate user", KnowledgeScope(workspace_id=ws_b), k=10)
    row_ids_a = {h.id for h in hits_a}
    row_ids_b = {h.id for h in hits_b}
    assert row_ids_a and row_ids_b
    assert row_ids_a.isdisjoint(row_ids_b)
    assert all(h.source_id == ids["a"][1] for h in hits_a)
    assert all(h.source_id == ids["b"][1] for h in hits_b)


def test_committed_floors_are_a_real_gate() -> None:
    """AC5: the committed floors are a meaningful (non-trivial, sub-perfect) gate."""
    assert 0.0 < DEFAULT_RECALL_FLOOR < 1.0
    assert 0.0 < DEFAULT_NDCG_FLOOR < 1.0


# --------------------------------------------------------------------------- #
# realeval — learned local embedder over the real repo corpus                  #
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def real_context():
    pytest.importorskip("sentence_transformers")
    embedder = resolve_embedder("local")
    reranker = resolve_reranker("fixture")
    return build_eval_context(
        embedder=embedder,
        reranker=reranker,
        corpus_root=repo_root(),
        embedder_name="local",
        reranker_name="fixture",
    )


@requires_realeval
@pytest.mark.realeval
def test_real_corpus_eval_reports_four_metrics(real_context) -> None:
    """AC2: recall@5, recall@10, MRR, nDCG@10 are all present and finite."""
    import math

    card = run_real_retrieval_eval(context=real_context, recall_floor=0.0, ndcg_floor=0.0)
    recall10 = mean_recall_at(
        real_context,
        # reuse the hybrid leg at k=10
        lambda case: [
            (c.path or "")
            for c in real_context.service.search(case.query, real_context.scope, k=10)
        ],
        10,
    )
    for value in (
        card.mean_recall_at_k,
        card.mean_mrr,
        card.mean_ndcg_at_k,
        recall10,
    ):
        assert math.isfinite(value)
        assert 0.0 <= value <= 1.0
    # nDCG is measured at 10 per the spec headline.
    assert card.ndcg_k == 10
    report = format_eval_report(card, {"hybrid": card}, recall_at_10=recall10)
    assert "recall@5" in report and "nDCG@10" in report and "MRR" in report


@requires_realeval
@pytest.mark.realeval
def test_real_numbers_are_not_perfect(real_context) -> None:
    """AC3: the measured real run lands in a realistic band, not a perfect 1.000."""
    card = run_real_retrieval_eval(context=real_context, recall_floor=0.0, ndcg_floor=0.0)
    assert 0.0 < card.mean_recall_at_k < 1.0
    assert not is_suspiciously_perfect(card), (
        "mean recall@5 >= 0.999 on the real corpus is a red flag (leakage/triviality)"
    )


@requires_realeval
@pytest.mark.realeval
def test_hybrid_beats_single_leg_ablation(real_context) -> None:
    """AC4: hybrid >= each single leg, with >=1 case only hybrid recovers."""
    ablation = run_ablation(context=real_context)
    hybrid = ablation["hybrid"]
    vector = ablation["vector_only"]
    keyword = ablation["keyword_only"]
    assert hybrid.mean_recall_at_k >= vector.mean_recall_at_k
    assert hybrid.mean_recall_at_k >= keyword.mean_recall_at_k

    def hits(card: Scorecard) -> dict[str, bool]:
        return {r.case_id: r.hit for r in card.results}

    h, v, kw = hits(hybrid), hits(vector), hits(keyword)
    hybrid_only = [cid for cid in h if h[cid] and not v.get(cid, False) and not kw.get(cid, False)]
    assert hybrid_only, "expected >=1 case recovered by hybrid that neither leg alone gets"


@requires_realeval
@pytest.mark.realeval
def test_regression_gate_from_real_baseline(real_context) -> None:
    """AC5: committed floors pass the real run; a degraded pipeline trips the gate."""
    card = run_real_retrieval_eval(
        context=real_context,
        recall_floor=DEFAULT_RECALL_FLOOR,
        ndcg_floor=DEFAULT_NDCG_FLOOR,
    )
    assert card.mean_recall_at_k >= DEFAULT_RECALL_FLOOR
    assert card.mean_ndcg_at_k >= DEFAULT_NDCG_FLOOR
    assert card.passed
    card.assert_threshold()

    # A deliberately broken retrieve_fn (returns nothing) must trip the gate.
    from forge_eval.runner import evaluate_retrieval

    broken = evaluate_retrieval(
        real_context.cases,
        lambda _case: [],
        k=5,
        ndcg_k=10,
        recall_threshold=DEFAULT_RECALL_FLOOR,
        ndcg_threshold=DEFAULT_NDCG_FLOOR,
    )
    assert not broken.passed
    with pytest.raises(AssertionError):
        broken.assert_threshold()


@requires_realeval
@pytest.mark.realeval
def test_local_embedder_no_call_time_network(monkeypatch) -> None:
    """AC6: with a warm cache the eval runs under HF_HUB_OFFLINE=1 (no network)."""
    pytest.importorskip("sentence_transformers")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    corpus = {
        "a/pool.py": "def connect(): # pooled postgres database connection\n    return 1\n",
        "a/auth.py": "def verify_jwt(token): # validate oauth jwt signature\n    return True\n",
    }
    embedder = resolve_embedder("local")
    service, scope = build_real_indexed_service(corpus, embedder, resolve_reranker("fixture"))
    hits = service.search("pooled database connection", scope, k=5)
    assert hits
    assert embedder.dimension > 0


@requires_realeval
@pytest.mark.realeval
def test_tenant_isolation_on_learned_embedder() -> None:
    """AC8: cross-tenant isolation holds on the learned-embedder path (row-id proof)."""
    pytest.importorskip("sentence_transformers")
    service, ids = _two_tenant_service(resolve_embedder("local"))
    hits_a = service.search("authenticate user", KnowledgeScope(workspace_id=ids["a"][0]), k=10)
    hits_b = service.search("authenticate user", KnowledgeScope(workspace_id=ids["b"][0]), k=10)
    assert {h.id for h in hits_a}.isdisjoint({h.id for h in hits_b})
    assert all(h.source_id == ids["a"][1] for h in hits_a)


# --------------------------------------------------------------------------- #
# Postgres-backed (AC13) — live pgvector; dim-match runs, dim-mismatch skips    #
# --------------------------------------------------------------------------- #

_PG_CORPUS = {
    "search/rrf.py": (
        "def fuse(rankings):\n"
        "    # reciprocal rank fusion combine rankings k=60\n"
        "    return sum(1.0 / (60 + r) for r in rankings)\n"
    ),
    "search/rerank.py": (
        "def rerank(query, docs):\n"
        "    # cross encoder reranker reorders candidate documents by relevance\n"
        "    return docs\n"
    ),
    "db/pool.py": (
        "def connect():\n    # pooled postgres database connection via psycopg\n    return pool\n"
    ),
}


@pytest.fixture
def pg_session_factory(pg_engine):
    from sqlalchemy.orm import Session, sessionmaker

    Base.metadata.create_all(pg_engine)
    try:
        yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(pg_engine)


@pytest.mark.postgres
def test_real_eval_on_pgvector_dim_matched(pg_session_factory) -> None:
    """AC13: with the embedder dim == the vector(N) column, the eval runs on real
    pgvector and its ranking matches the hermetic SQLite run (within tolerance)."""
    import uuid

    from forge_db.models.knowledge import EMBEDDING_DIM

    embedder = DeterministicEmbeddingClient(dimension=EMBEDDING_DIM)
    query = "reciprocal rank fusion combine rankings"

    pg_service, pg_scope = build_real_indexed_service(
        _PG_CORPUS,
        embedder,
        FixtureRerankerClient(),
        session_factory=pg_session_factory,
        workspace_slug=f"eval-pg-{uuid.uuid4().hex[:8]}",
    )
    pg_hits = pg_service.search(query, pg_scope, k=3)
    assert pg_hits, "expected pgvector-backed results"
    assert all(h.score >= 0.0 for h in pg_hits)

    # Same corpus + embedder on hermetic SQLite: top-1 path must agree.
    sq_service, sq_scope = build_real_indexed_service(
        _PG_CORPUS, DeterministicEmbeddingClient(dimension=EMBEDDING_DIM), FixtureRerankerClient()
    )
    sq_hits = sq_service.search(query, sq_scope, k=3)
    assert sq_hits
    assert pg_hits[0].path == sq_hits[0].path == "search/rrf.py"


@pytest.mark.postgres
def test_pgvector_rejects_dim_mismatch_never_coerces(pg_session_factory) -> None:
    """AC13: a dim that differs from the vector(N) column is NEVER silently
    truncated/padded. The learned 384-dim model does not match the fixed
    vector(1536) column, so on Postgres the eval must skip (dim-reconciliation is
    HARD-03's migration) rather than coerce — proven here by pgvector *rejecting*
    a mismatched-dim index attempt with a hard error instead of padding it."""
    import uuid

    from sqlalchemy.exc import SQLAlchemyError

    from forge_db.models.knowledge import EMBEDDING_DIM

    learned_dim = 384  # all-MiniLM-L6-v2
    assert learned_dim != EMBEDDING_DIM, "this guard only matters when dims differ"
    with pytest.raises(SQLAlchemyError):
        build_real_indexed_service(
            _PG_CORPUS,
            DeterministicEmbeddingClient(dimension=learned_dim),
            FixtureRerankerClient(),
            session_factory=pg_session_factory,
            workspace_slug=f"eval-mismatch-{uuid.uuid4().hex[:8]}",
        )
