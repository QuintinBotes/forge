# HARD-04 — Real RAG Eval on a Real Corpus (honest recall@k / MRR / nDCG)

> Phase: hardening · Blocker(s): #2 (eval numbers offline/deterministic — fake embedder + fixture reranker, perfect 1.000s) · Status target: **DONE/verified when** `forge_eval` reports recall@5, recall@10, MRR, and nDCG@10 computed by the *real* hybrid pipeline using a **learned local `sentence-transformers` embedder (no API key required)** over a **real, heterogeneous corpus** (the Forge monorepo + a curated corpus), the numbers are honest (a perfect 1.000 across the board is treated as a red flag to investigate, not a pass), a documented **regression gate is set from the real baseline**, an **ablation proves hybrid beats vector-only and keyword-only**, the Track 1.4 adversarial refutation is re-reviewed and resolved, and `MORNING_REPORT.md §4`'s deterministic 1.000 headline is superseded and relabeled "wiring check only". **No external creds required** for the BETA-critical path (local embedder); a BYOK reranker (Jina/Cohere) is *optional* to also report hosted-reranker deltas. The whole-suite green gate holds at the end of the workstream.

---

## 1. Intent — what & why

The ALPHA's headline retrieval numbers — `recall@5 = 1.000, MRR = 1.000` (`MORNING_REPORT.md §4`) — are perfect *by construction*, and the report says so honestly: they come from a **deterministic offline pipeline** (`DeterministicEmbeddingClient`, a signed feature-hashing bag-of-words, + `FixtureRerankerClient`, a token-overlap fake) over a **20-file synthetic corpus** (`forge_eval.retrieval_eval.SAMPLE_CORPUS`) with a **22-case golden set** whose queries were written alongside the corpus, scored on an **in-memory SQLite** backend that computes cosine/BM25 in Python. That proves the *wiring and ranking logic* are correct end-to-end. It does **not** measure retrieval quality with a learned embedding model on a large heterogeneous corpus — which is exactly what blocker #2 demands before any number is quoted externally.

HARD-04 makes the eval honest. It re-runs the **same real `KnowledgeService` pipeline** (semantic pgvector leg + BM25 keyword leg → RRF k=60 → cross-encoder rerank → attributed top-k) but swaps the two stand-ins for real models on a real corpus:

1. **Learned embedder, no key.** A local open-weight `sentence-transformers` model (the default-real embedder delivered by **HARD-03**, e.g. `BAAI/bge-small-en-v1.5`, 384-dim) embeds the corpus and queries. This gives genuine semantic recall numbers with **zero API spend and zero network at call time** once the model is cached — the single highest-signal BETA deliverable for blocker #2.
2. **Real, heterogeneous corpus.** Index the actual Forge monorepo (Python under `packages/**` + `apps/**`, markdown under `docs/**`, READMEs, `examples/sample-repo`) through the real ingestion path (`forge_knowledge.sync`), not a hand-built dict whose answers were authored next to the questions.
3. **Honest metrics + the missing one.** Report recall@5, recall@10, MRR, and add **nDCG@10** (rank-quality, currently absent from `forge_eval.metrics`). A perfect 1.000 on a real corpus is a *red flag* (leakage / trivial golden set), not a pass.
4. **Ablation.** Measure hybrid vs vector-only vs keyword-only on the *same* corpus and golden set, proving fusion adds recall over either leg alone (the central design claim of F05).
5. **Regression gate from reality.** Replace the conservative offline floor (`DEFAULT_RECALL_THRESHOLD = 0.85`, hand-set for the deterministic pipeline) with a baseline derived from the *measured real* numbers, wired so future drops block CI.
6. **Close the parked review.** Re-examine the Track 1.4 adversarial refutation (`forge_knowledge.sync` + `forge_eval.retrieval_eval`) flagged in `MORNING_REPORT.md §6` ("1 refutation, repaired=false … deserves a fresh look") and mark it resolved-or-invalid with a one-line rationale in the eval doc.

This **extends** the existing `forge_eval` (`packages/evaluation`) and `forge_knowledge` (`packages/knowledge-core`) packages — no new package, no fork of the pipeline. The pipeline under test is byte-for-byte the production one; only the injected `EmbeddingClient` / `RerankerClient` and the corpus change, exactly as the contracts (`forge_contracts.protocols.EmbeddingClient` / `RerankerClient`) were designed to allow.

## 2. User-facing / operator behavior

HARD-04 has no end-user UI; its "users" are maintainers, reviewers, and CI. Observable behavior:

- **Operator journey A — run the honest eval locally, no key.** A maintainer runs `uv run python -m forge_eval.corpus_eval` (or `uv run pytest -m realeval -s`). The harness loads the cached local `sentence-transformers` model, indexes the repo corpus through the real `KnowledgeService`, scores the curated real golden set, and prints a scorecard with recall@5/recall@10/MRR/**nDCG@10**, the ablation table (hybrid vs vector-only vs keyword-only), and a PASS/FAIL against the real regression floor. First run downloads the model to the HF cache (`HF_HOME`/`SENTENCE_TRANSFORMERS_HOME`); later runs are offline.
- **Operator journey B — report hosted-reranker delta (optional, creds).** With `JINA_API_KEY` (or `COHERE_API_KEY`) + `JINA_RERANKER_URL` in `.env.integration`, `FORGE_EVAL_RERANKER=jina` re-runs the same eval through the real reranker and prints the **reranker delta** (recall/nDCG with vs without the learned cross-encoder) — the FORGE_SPEC "reranker delta" observability metric, now measured for real.
- **Operator journey C — CI regression gate.** The `realeval` CI lane runs the local-embedder eval on a cached model on every PR; if mean recall@5 (or nDCG@10) drops below the committed real baseline, the job fails with the offending cases listed. The published `EVAL_REPORT.md` artifact carries the current numbers and supersedes the `MORNING_REPORT.md §4` headline.
- **Operator journey D — read the honest headline.** Anyone reading `MORNING_REPORT.md §4` or `packages/evaluation/README.md` sees the deterministic 1.000s **relabeled** "wiring check only — see `EVAL_REPORT.md` for real-corpus numbers", with the real recall/MRR/nDCG as the quoted figures.

## 3. Vertical slice

### 3.1 Data model

No schema change. HARD-04 reads and writes only the existing `forge_db` tables the pipeline already uses — singular, str-enum tables per the real schema:

- `knowledge_source` (model `forge_db.models.KnowledgeSource`) — one row per eval corpus.
- `retrieval_chunk` (model `forge_db.models.RetrievalChunk`) — the indexed chunks; `embedding` column is `Vector(EMBEDDING_DIM).with_variant(JSON(), "sqlite")` with `forge_db.models.knowledge.EMBEDDING_DIM = 1536`.

Two backend modes, both exercised:

- **Hermetic SQLite (default for `realeval`).** `create_engine("sqlite://")` + `Base.metadata.create_all` (exactly as `build_indexed_service` already does). The `embedding` JSON variant accepts **any** vector length, so a 384-dim local model indexes without a migration — this is what makes the no-creds, no-Postgres real-eval path possible. Cosine/BM25 are computed in Python by the dialect-aware stores; ranking semantics are identical to Postgres (RRF is rank-based).
- **Real pgvector (when `FORGE_TEST_DATABASE_URL` set — shared with HARD-01).** Uses the root `conftest.py` `pg_engine` fixture. **Dim caveat:** the `vector(N)` column is fixed at `EMBEDDING_DIM = 1536`; a 384-dim local model only indexes on Postgres if the column dim matches. HARD-03 owns the dim-reconciliation migration note; HARD-04 consumes it: on Postgres the eval uses an embedder whose `dimension` equals the live column dim (or the HARD-03 `EMBEDDING_DIM` override), and **skips with a clear reason** if they differ — never silently truncates/pads.

`metadata`/`source_uri` on `retrieval_chunk` already carry `path` (the golden set's ground-truth id), so no denormalization is needed.

### 3.2 Backend

Extends `packages/evaluation/forge_eval` (the `forge_eval` package). No FastAPI route — the eval is a library + CLI + CI artifact, consistent with the existing `forge_eval.retrieval_eval` design. New/changed modules:

- **`forge_eval/metrics.py` (extend).** Add `ndcg_at_k(retrieved, relevant, k)` (binary-gain DCG with graded-gain support via an optional `gains` map; IDCG over the ideal ordering). Pure, deterministic, no I/O — mirrors the existing `recall_at_k`/`reciprocal_rank` style and `__all__` export discipline.
- **`forge_eval/runner.py` (extend).** Add `ndcg_at_k: float` to `CaseResult`; add `mean_ndcg_at_k` to `Scorecard`; have `evaluate_retrieval` populate it. Add an optional **graded relevance** path: `GoldenCase.metadata["gains"]` (id→gain) feeds nDCG when present, else binary. Keep the existing recall-threshold gate; add an `ndcg_threshold` field (default `0.0` = off) so the real gate can assert both.
- **`forge_eval/real_corpus.py` (new).** Builds the real corpus from the live repo via the **real ingestion path**, reusing `forge_knowledge.sync.read_repo_files` / `iter_source_files` (which already apply `DEFAULT_EXCLUDE_DIRS`, `DEFAULT_EXCLUDE_SUFFIXES`, `DEFAULT_MAX_FILE_BYTES`, and skip binaries/VCS metadata):
  - `build_repo_corpus(root, *, include_globs, exclude_globs, max_bytes) -> dict[str, str]`
  - `build_real_indexed_service(corpus, embedding_client, reranker, *, session_factory=None) -> tuple[KnowledgeService, KnowledgeScope]` — same shape as the existing `build_indexed_service`, but corpus-driven and embedder/reranker-injected; defaults to in-memory SQLite, accepts a Postgres `session_factory`.
- **`forge_eval/corpus_eval.py` (new).** The honest-eval orchestrator + CLI (`python -m forge_eval.corpus_eval`):
  - `resolve_embedder(spec: str) -> EmbeddingClient` — `local|http` (local = HARD-03 `SentenceTransformerEmbeddingClient`, resolved by env `SENTENCE_TRANSFORMERS_MODEL`; http = `HttpEmbeddingClient` BYOK).
  - `resolve_reranker(spec: str) -> RerankerClient` — `fixture|jina|cohere` (jina/cohere = `JinaRerankerClient` BYOK; key from env/vault, never logged).
  - `run_real_retrieval_eval(*, embedder, reranker, k=5, search_k=10, recall_floor, ndcg_floor) -> Scorecard`.
  - `run_ablation(*, embedder, reranker, k, search_k) -> dict[str, Scorecard]` with keys `hybrid|vector_only|keyword_only` (vector-only calls `HybridRetriever.semantic`; keyword-only calls `.keyword`; hybrid is the full `KnowledgeService.search`).
  - `format_eval_report(scorecards, ablation) -> str` → written to `EVAL_REPORT.md`.
- **`forge_eval/retrieval_eval.py` (extend, do not break).** Keep `SAMPLE_CORPUS` + `build_indexed_service` (now explicitly documented as the **wiring check**, not the headline). Add a module docstring pointer to `corpus_eval`. Add a one-line **Track 1.4 resolution note** (see §6 AC10) citing the re-review outcome.
- **`forge_eval/report.py` (extend).** Add an nDCG column to `format_scorecard` and an `format_ablation(ablation)` helper.

The golden set, redaction, and tenant-scoping all flow through the existing frozen contracts (`forge_contracts.dtos.KnowledgeScope`, `Chunk`, `RetrievedChunk`); nothing in the frozen surface changes.

### 3.3 Worker / agent

None. HARD-04 is an eval/measurement workstream; it does not add Celery tasks, agent graph nodes, or worker code. (Retrieval *latency* p50/p95/p99 on the real embedder+pgvector — the perf dimension — is **HARD-13**, not here; HARD-04 is quality, not latency.)

### 3.4 Frontend

None. The eval is a CLI + CI artifact (`EVAL_REPORT.md`) + the relabeled `MORNING_REPORT.md §4` / `packages/evaluation/README.md`. No Next.js route or component. (Surfacing reranker-delta/recall as a live observability widget is out of scope — owned by the Observability slice; see §12.)

### 3.5 Infra / deploy / CI

- **CI lane `realeval`** added to `.github/workflows/ci.yml` (a new job, parallel to the existing `Test (pytest)` job): installs the `eval` extra (pulls HARD-03's `sentence-transformers`), restores the model from an `actions/cache` keyed on `SENTENCE_TRANSFORMERS_MODEL`, then runs `uv run pytest -m realeval` + `uv run python -m forge_eval.corpus_eval --write-report`. It uploads `EVAL_REPORT.md` as a build artifact and **fails on regression** below the committed real baseline. No DB service required (SQLite path); when the existing `postgres` service is present it additionally runs the pgvector-backed variant if the dim matches.
- **Model caching.** `HF_HOME` / `SENTENCE_TRANSFORMERS_HOME` point at the cached dir so call-time is offline; first CI run (cache miss) downloads once. `HF_HUB_OFFLINE=1` is set for the assertion that no network occurs at call time after warm cache.
- **No image/registry work** here (that is HARD-08). No new compose service.
- **pyproject markers.** Add `realeval` to `[tool.pytest.ini_options].markers` in root `pyproject.toml` (alongside the existing `postgres` and `integration` markers) so the heavy/learned-model eval is opt-in and the default `uv run pytest -q` stays hermetic and fast.
- **Optional `eval` extra.** `packages/evaluation/pyproject.toml` gains an `[project.optional-dependencies] eval = ["sentence-transformers>=3.0"]` (the actual dependency is declared by HARD-03 on `forge_knowledge`; `forge_eval` references it via the local embedder it imports). Re-lock (`uv lock`) is owned by **HARD-14**.

## 4. Public interfaces / contracts (exact signatures, env vars, config keys)

**`forge_eval/metrics.py`** (new function; pure):

```python
def ndcg_at_k(
    retrieved: Sequence[str],
    relevant: Collection[str],
    k: int,
    *,
    gains: Mapping[str, float] | None = None,   # id -> graded gain; default binary 1.0
) -> float:
    """Normalised DCG@k. DCG = Σ gain_i / log2(rank_i + 1) over the top-k;
    IDCG = same over the ideal (gain-desc) ordering. Returns 1.0 when there are
    no relevant ids; 0.0 when none of the top-k are relevant."""
```

**`forge_eval/runner.py`** (extended dataclasses — additive, back-compatible):

```python
@dataclass
class CaseResult:
    case_id: str
    retrieved_ids: list[str]
    recall_at_k: float
    precision_at_k: float
    reciprocal_rank: float
    ndcg_at_k: float            # NEW
    hit: bool
    passed: bool

@dataclass
class Scorecard:
    k: int
    recall_threshold: float
    ndcg_threshold: float = 0.0   # NEW; 0.0 == gate disabled (back-compat)
    results: list[CaseResult] = field(default_factory=list)
    # NEW property:
    @property
    def mean_ndcg_at_k(self) -> float: ...
    # gate now: mean_recall_at_k >= recall_threshold AND mean_ndcg_at_k >= ndcg_threshold
```

**`forge_eval/real_corpus.py`** (new):

```python
def build_repo_corpus(
    root: str | Path,
    *,
    include_globs: Sequence[str] = ("packages/**/*.py", "apps/**/*.py",
                                    "docs/**/*.md", "**/README.md", "examples/**/*"),
    exclude_globs: Sequence[str] = (),   # merged with forge_knowledge.sync defaults
    max_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> dict[str, str]: ...

def build_real_indexed_service(
    corpus: dict[str, str],
    embedding_client: EmbeddingClient,
    reranker: RerankerClient,
    *,
    session_factory: SessionFactory | None = None,   # None -> in-memory SQLite
) -> tuple[KnowledgeService, KnowledgeScope]: ...
```

**`forge_eval/corpus_eval.py`** (new):

```python
def resolve_embedder(spec: str | None = None) -> EmbeddingClient: ...   # "local"|"http"
def resolve_reranker(spec: str | None = None) -> RerankerClient: ...    # "fixture"|"jina"|"cohere"
def run_real_retrieval_eval(
    *, embedder: EmbeddingClient, reranker: RerankerClient,
    corpus_root: str | Path | None = None, golden_path: str | Path | None = None,
    k: int = 5, search_k: int = 10,
    recall_floor: float, ndcg_floor: float,
) -> Scorecard: ...
def run_ablation(
    *, embedder: EmbeddingClient, reranker: RerankerClient,
    corpus_root: str | Path | None = None, golden_path: str | Path | None = None,
    k: int = 5, search_k: int = 10,
) -> dict[str, Scorecard]: ...   # keys: "hybrid", "vector_only", "keyword_only"
def main(argv: list[str] | None = None) -> int: ...   # CLI: --write-report, --embedder, --reranker, --k
```

**Golden set (new file):** `packages/evaluation/forge_eval/data/golden_retrieval_real.json` — same schema the existing `forge_eval.golden.load_golden_set` parses (`{cases: [{id, query, expected_ids, kind, tags, metadata?}]}`). `expected_ids` are real repo-relative paths in the indexed corpus; `metadata.gains` is optional graded relevance. ≥ 30 curated cases (mix of natural-language semantic, exact-identifier, and cross-file queries), authored against the real repo, not the synthetic corpus.

**Env vars / config keys** (names only; secret values live in gitignored `.env.integration`):

| Key | Default | Meaning |
|---|---|---|
| `FORGE_EVAL_EMBEDDER` | `local` | `local` (sentence-transformers, no key) \| `http` (BYOK OpenAI-compatible) |
| `SENTENCE_TRANSFORMERS_MODEL` | `BAAI/bge-small-en-v1.5` | local model id (HARD-03) |
| `EMBEDDING_MODEL` / `EMBEDDING_PROVIDER` | — | for the `http` BYOK embedder |
| `OPENAI_API_KEY` | — | BYOK embedder key (optional path) |
| `FORGE_EVAL_RERANKER` | `fixture` | `fixture` \| `jina` \| `cohere` |
| `JINA_API_KEY` / `COHERE_API_KEY` | — | BYOK reranker key (optional) |
| `JINA_RERANKER_URL` | — | self-hosted/hosted reranker base URL |
| `FORGE_EVAL_CORPUS_ROOT` | repo root | corpus root to index |
| `FORGE_EVAL_RECALL_FLOOR` | committed baseline | recall@5 regression floor |
| `FORGE_EVAL_NDCG_FLOOR` | committed baseline | nDCG@10 regression floor |
| `HF_HOME` / `SENTENCE_TRANSFORMERS_HOME` | CI cache dir | model cache (offline at call time) |
| `HF_HUB_OFFLINE` | `1` (warm cache) | assert no call-time network |
| `FORGE_TEST_DATABASE_URL` | unset | optional live pgvector (HARD-01 path) |

## 5. Dependencies (other slices/foundation that must exist first)

- **HARD-03 — Real embedder + reranker (REQUIRED).** Delivers the local `sentence-transformers` `EmbeddingClient` (`SentenceTransformerEmbeddingClient`, default-real, no key) and the wired BYOK `JinaRerankerClient`/Cohere reranker, plus the embedding-dim reconciliation note. HARD-04 *consumes* these behind the frozen `forge_contracts.protocols.EmbeddingClient`/`RerankerClient`; it does not implement the model loader.
- **HARD-01 — Real Postgres + pgvector (SOFT / for the pg-backed variant only).** Needed only to run the eval against live pgvector via `FORGE_TEST_DATABASE_URL` + the `pg_engine` fixture. The BETA-critical local-embedder eval runs hermetically on in-memory SQLite without it.
- **Foundation already present (no work):** `forge_eval` (`packages/evaluation`) with `metrics`/`runner`/`golden`/`retrieval_eval`/`report`; `forge_knowledge` `KnowledgeService` + `HybridRetriever` + `stores` + `sync.read_repo_files`; frozen `forge_contracts` (`EmbeddingClient`, `RerankerClient`, `KnowledgeScope`, `Chunk`, `RetrievedChunk`); root `conftest.py` `pg_engine`/`postgres_url` fixtures; `pyproject` pytest markers block.
- **HARD-14 — Dependency re-lock (DOWNSTREAM).** Re-locks `uv.lock` once the `eval`/`sentence-transformers` extra is declared; HARD-04 adds the extra declaration, HARD-14 freezes it for CI `--frozen`.

## 6. Acceptance criteria (numbered, testable)

> "offline" = runs in the hermetic no-network sandbox after the model is cached (no external creds). "creds" = requires a BYOK key. The whole-suite green gate (`uv run pytest -q` + `ruff check .` + `ruff format --check .` + `make typecheck` + `cd apps/web && pnpm test`) must be green at workstream end (offline).

1. **(offline)** `ndcg_at_k` matches hand-computed values: for a single relevant id at rank `r`, `ndcg@k = 1/log2(r+1)` for `r ≤ k`, `0.0` for `r > k`; graded `gains` change the score; empty `relevant` → `1.0`. Unit-tested with known fixtures.
2. **(offline)** `run_real_retrieval_eval` with the **local `sentence-transformers` embedder** over the real repo corpus reports `recall@5`, `recall@10`, `MRR`, and `nDCG@10`; all four are written to `EVAL_REPORT.md`. (No key; model from cache.)
3. **(offline)** The reported numbers are **honest, not perfect**: mean recall@5 is in a realistic band (`0.0 < x < 1.0`) and a mean recall@5 == 1.000 across all cases **fails** a guard test that flags it as suspected leakage/triviality to investigate (the red-flag check, not a silent pass).
4. **(offline)** Ablation: `run_ablation` returns `hybrid|vector_only|keyword_only` scorecards on the *same* corpus + golden set, and `hybrid.mean_recall_at_k >= max(vector_only, keyword_only).mean_recall_at_k` with at least one curated case recovered by hybrid that a single leg misses (proves fusion adds recall). Recorded in the report.
5. **(offline)** A **regression gate from the real baseline** is committed (`FORGE_EVAL_RECALL_FLOOR`, `FORGE_EVAL_NDCG_FLOOR`, set just below the measured real numbers); `Scorecard.assert_threshold` fails when measured recall@5 *or* nDCG@10 drops below its floor, and the CI `realeval` lane is wired to it.
6. **(offline)** The local embedder performs **no network I/O at call time** with a warm cache: a test runs the eval under `HF_HUB_OFFLINE=1` and asserts it completes (model loaded from cache, embeddings computed locally).
7. **(offline)** The curated real golden set `golden_retrieval_real.json` has ≥ 30 cases; every `expected_ids` path exists in the indexed real corpus; ids are unique; queries mix semantic / exact-identifier / cross-file kinds (tag distribution asserted).
8. **(offline)** Tenant isolation holds on the real corpus: a query scoped to workspace A never returns workspace B chunks even with identical content indexed under both (row-id assertion) — re-proves the F05 AC6 property on the learned-embedder path.
9. **(creds, optional)** With `FORGE_EVAL_RERANKER=jina` (or `cohere`) and a BYOK key from env, the eval re-runs through the **real reranker** and reports the **reranker delta** (recall@5 / nDCG@10 with vs without rerank); the key never appears in `EVAL_REPORT.md`, logs, or any scorecard. Skips cleanly when the key is absent.
10. **(offline)** The **Track 1.4 adversarial refutation** (`forge_knowledge.sync` + `forge_eval.retrieval_eval`, `MORNING_REPORT.md §6`) is explicitly re-reviewed; a one-line dated rationale ("resolved: …" or "invalid: …") is recorded in `forge_eval/retrieval_eval.py`'s docstring and `EVAL_REPORT.md`, and any genuine defect it surfaced is fixed with a regression test.
11. **(offline)** `MORNING_REPORT.md §4` and `packages/evaluation/README.md` are updated: the deterministic `1.000` numbers are relabeled "wiring check only" and the real recall/MRR/nDCG (with the embedder + corpus identified) become the quoted headline, pointing at `EVAL_REPORT.md`.
12. **(offline)** The existing deterministic eval (`build_indexed_service` / `run_retrieval_eval` / `test_retrieval_eval.py`) still passes unchanged (the wiring check is preserved, not deleted); `forge_eval` default tests stay hermetic and network-free.
13. **(creds/SOFT, pg)** When `FORGE_TEST_DATABASE_URL` points at live pgvector **and** the embedder dim matches the `vector(N)` column, the same eval runs against Postgres and produces metrics consistent (within tolerance) with the SQLite run; mismatched dim **skips with a clear reason**, never silently coerces.
14. **(offline)** No real secret appears anywhere in the corpus, golden set, scorecards, or report: corpus building excludes `.env*`, `*.pem`, `*.key`, `deploy/secrets/**`, and chunk content passes the shared redaction filter before persistence (re-proving F05 AC11 on real files).

## 7. Test plan (TDD) — unit + integration (gated on env creds) + how to run

Tests live in `packages/evaluation/tests/`. Write the metric and gate tests first (red), then the corpus/embedder wiring.

**Unit (pure, hermetic — run in the default suite):**
- `test_ndcg_at_k_matches_hand_computed` (AC1) — rank-1 → 1.0; rank-3, k=5 → `1/log2(4)`; rank beyond k → 0.0; graded gains; empty-relevant → 1.0.
- `test_scorecard_ndcg_mean_and_dual_gate` — `mean_ndcg_at_k`; gate fails if either recall or nDCG below floor; back-compat when `ndcg_threshold=0.0`.
- `test_real_golden_set_valid` (AC7) — ≥30 cases, unique ids, every `expected_ids` path present in `build_repo_corpus(repo_root)`, tag mix present.
- `test_deterministic_eval_unchanged` (AC12) — existing `run_retrieval_eval` still passes; relabel docstring present.

**`realeval`-marked (offline after warm cache; needs the local model — `@pytest.mark.realeval`, gated by `pytest.importorskip("sentence_transformers")`):**
- `test_real_corpus_eval_reports_four_metrics` (AC2) — recall@5/recall@10/MRR/nDCG@10 all present and finite.
- `test_real_numbers_are_not_perfect` (AC3) — guard fails a synthetic all-1.000 scorecard; real run lands in `(0,1)`.
- `test_hybrid_beats_single_leg_ablation` (AC4) — hybrid ≥ each leg; ≥1 hybrid-only recovery case.
- `test_regression_gate_from_real_baseline` (AC5) — floors enforced; a deliberately degraded retrieve_fn trips the gate.
- `test_local_embedder_no_call_time_network` (AC6) — under `HF_HUB_OFFLINE=1` the eval completes.
- `test_tenant_isolation_on_real_corpus` (AC8).
- `test_redaction_holds_on_real_files` (AC14) — seed a file with a fake `AKIA…`/PEM block in a temp corpus; assert `«redacted»` in stored chunk and no secret substring in any result.

**Integration (creds-gated — `@pytest.mark.integration`, skip-clean without keys):**
- `test_real_reranker_delta` (AC9) — BYOK Jina/Cohere; reports delta; asserts no key leak in report/scorecard.

**Postgres-backed (`@pytest.mark.postgres`, skip without `FORGE_TEST_DATABASE_URL`):**
- `test_real_eval_on_pgvector_when_dim_matches` (AC13) — runs on `pg_engine`; dim-mismatch skip path asserted.

**How to run:**
```bash
# Default hermetic suite (unit metrics + gate + golden validity), no model, no network:
uv run pytest packages/evaluation -q

# Honest real-corpus eval (local embedder; first run downloads model, then offline):
uv run pytest -m realeval -s
uv run python -m forge_eval.corpus_eval --write-report   # writes EVAL_REPORT.md

# Optional hosted-reranker delta (needs .env.integration):
FORGE_EVAL_RERANKER=jina uv run pytest -m "realeval and integration" -s

# Optional pgvector variant (needs HARD-01 DB + matching dim):
export FORGE_TEST_DATABASE_URL=postgresql+psycopg://forge:forge@localhost:5432/forge_test
uv run pytest -m "realeval and postgres" -s
```

## 8. Security & policy considerations

- **Corpus must not ingest secrets.** `build_repo_corpus` excludes `.env*`, `*.pem`, `*.key`, `deploy/secrets/**`, `.git/**` and reuses `forge_knowledge.sync`'s `DEFAULT_EXCLUDE_DIRS`/`DEFAULT_EXCLUDE_SUFFIXES`/`DEFAULT_MAX_FILE_BYTES`. Every chunk still passes the shared redaction filter before persistence (defense-in-depth; AC14). No real `.env.integration` or `github-app.pem` content can ever reach a chunk, scorecard, or `EVAL_REPORT.md`.
- **BYOK keys from env/vault, never logged.** The optional `http` embedder and `jina`/`cohere` reranker resolve keys from env (sourced from gitignored `.env.integration`) or the per-workspace vault at call time and discard them; keys never enter the report, scorecards, fixtures, lockfile, or CI logs. The shared redaction filter is re-applied to any error/diagnostic the eval prints.
- **No secret values in committed artifacts.** `EVAL_REPORT.md`, `golden_retrieval_real.json`, and the relabeled `MORNING_REPORT.md §4` contain only metrics, model ids, and query/path text — never a key. CI's secret-scan (HARD-09) covers these paths.
- **Deterministic, reproducible, no hidden network.** Local embedder runs offline at call time (`HF_HUB_OFFLINE=1` asserted, AC6); the eval never silently falls back to the fake embedder on the `realeval` lane (if the model is unavailable the test *skips*, it does not fake-pass), preserving the honesty contract from the spec.
- **Tenant isolation re-proven** on the learned-embedder path (AC8), so the quality work cannot accidentally regress the F05 cross-tenant guarantee.

## 9. Effort & risk (S/M/L + risks)

**Effort: M.** New metric + gate (S), real corpus builder reusing `sync` (S), corpus/ablation orchestrator + CLI + report (M), curated ≥30-case real golden set (M — the human-judgment cost is authoring honest query→path labels), CI lane + caching + relabel docs (S). Depends on HARD-03 landing the local embedder first.

**Risks:**
- **Model download / cache flakiness in CI.** Mitigation: `actions/cache` keyed on model id; `HF_HUB_OFFLINE=1` after warm cache; small default model (`bge-small`, ~130MB). (Medium)
- **Embedding-dim vs `vector(1536)` column mismatch on Postgres.** Mitigation: SQLite JSON variant is dim-agnostic (BETA path); pg variant skips on mismatch and defers the dim-reconciliation migration to HARD-03. (Medium)
- **Golden-set leakage / triviality** (queries that echo the file → fake-perfect scores). Mitigation: AC3 red-flag guard; queries authored to paraphrase, not quote; tag-balanced; reviewed. (Medium — this is the central honesty risk the workstream exists to retire.)
- **Corpus nondeterminism** (repo changes shift numbers). Mitigation: pin the curated golden set to stable files; floors set with margin; report records the corpus commit + file count. (Low-Med)
- **Defining a fair real floor.** Too high → flaky CI; too low → meaningless gate. Mitigation: set floors just below the measured real mean with a documented margin; record the baseline + date in `EVAL_REPORT.md`. (Low-Med)
- **CANNOT be done in-sandbox:** the initial model download needs network (one-time, on a networked runner/CI); the hosted-reranker delta (AC9) needs a real BYOK key; the pg-backed variant (AC13) needs HARD-01's live Postgres. None require a human. No part of HARD-04 needs the external human pentest or fleet soak (those belong to HARD-09 / HARD-13).

## 10. Key files / paths (exact, in the real monorepo)

- `packages/evaluation/forge_eval/metrics.py` — **extend**: add `ndcg_at_k`.
- `packages/evaluation/forge_eval/runner.py` — **extend**: `CaseResult.ndcg_at_k`, `Scorecard.mean_ndcg_at_k` + `ndcg_threshold` dual gate.
- `packages/evaluation/forge_eval/real_corpus.py` — **new**: `build_repo_corpus`, `build_real_indexed_service`.
- `packages/evaluation/forge_eval/corpus_eval.py` — **new**: resolvers, `run_real_retrieval_eval`, `run_ablation`, `main` CLI, report writer.
- `packages/evaluation/forge_eval/retrieval_eval.py` — **extend**: relabel as wiring-check; Track 1.4 resolution note.
- `packages/evaluation/forge_eval/report.py` — **extend**: nDCG column + `format_ablation`.
- `packages/evaluation/forge_eval/data/golden_retrieval_real.json` — **new**: ≥30 curated real cases.
- `packages/evaluation/tests/test_metrics.py` — **extend**: nDCG unit tests.
- `packages/evaluation/tests/test_corpus_eval.py` — **new**: real-eval, ablation, gate, red-flag, isolation, redaction tests (`realeval`/`integration`/`postgres` marked).
- `packages/evaluation/tests/test_retrieval_eval.py` — **extend**: assert deterministic eval unchanged + relabel present.
- `packages/evaluation/pyproject.toml` — **extend**: `[project.optional-dependencies] eval`.
- `packages/knowledge-core/forge_knowledge/embeddings.py` — local `SentenceTransformerEmbeddingClient` (delivered by **HARD-03**; consumed here).
- `packages/knowledge-core/forge_knowledge/sync.py` — `read_repo_files` / `iter_source_files` reused (no change; Track 1.4 re-review target).
- `pyproject.toml` (root) — **extend**: add `realeval` pytest marker.
- `.github/workflows/ci.yml` — **extend**: `realeval` job + model cache + artifact upload + regression gate.
- `docs/MORNING_REPORT.md` §4 — **extend**: relabel deterministic numbers; quote real headline.
- `packages/evaluation/README.md` — **extend**: same relabel; how-to-run the honest eval.
- `EVAL_REPORT.md` (repo root) — **new artifact**: real numbers + ablation + baseline + corpus commit.
- `conftest.py` (root) — reused `pg_engine`/`postgres_url` (no change).

## 11. Research references

- IR metrics — recall@k, MRR, nDCG (DCG/IDCG, log2 discount): Manning, Raghavan & Schütze, *Introduction to Information Retrieval*, ch. 8 (evaluation); Järvelin & Kekäläinen, "Cumulated gain-based evaluation of IR techniques" (nDCG origin).
- BEIR — heterogeneous zero-shot retrieval benchmark methodology (why a real, heterogeneous corpus matters): https://github.com/beir-cellar/beir
- `sentence-transformers` + MTEB leaderboard (open-weight embedder selection, `bge-small/base`): https://www.sbert.net/ and https://huggingface.co/spaces/mteb/leaderboard
- pgvector cosine + HNSW (the semantic leg under test): https://github.com/pgvector/pgvector
- Reciprocal Rank Fusion (k=60): Cormack, Clarke & Büttcher, "Reciprocal Rank Fusion outperforms Condorcet…".
- Reranker quality lift (Jina Reranker v2, cross-encoder delta): https://jina.ai/reranker/ ; Cohere Rerank: https://docs.cohere.com/docs/rerank-overview
- Spec sections: `docs/FORGE_SPEC.md` → "Knowledge and Retrieval Architecture" (pipeline, chunk weights), Technology Stack rows (pgvector cosine, Postgres BM25, RRF k=60, Jina Reranker v2), Observability metrics ("reranker delta", "retrieval latency p50/p95/p99").
- Ground truth: `docs/MORNING_REPORT.md` §4 (the 1.000 honesty caveat), §6 (Track 1.4 refutation, eval-realism gap), §7 step 4 (re-run on real embedder/reranker/corpus); `docs/implementation-slices/v1/F05-hybrid-knowledge-retrieval.md` (pipeline + AC4 fusion-adds-recall + eval hook); `scratchpad/hardening-docs/SPEC-PRODUCTION-HARDENING.md` (HARD-03/04, G-RAG-REAL gate, BETA DoD item 4).

## 12. Out of scope / future

- **Retrieval latency p50/p95/p99 on the real embedder + pgvector** (the *perf* dimension) — **HARD-13**; HARD-04 measures quality, not latency.
- **Hosted/BYOK embedder MTEB-scale benchmarking** and embedder A/B (OpenAI vs Jina vs Voyage at corpus scale) — future eval expansion; HARD-04 ships local-default + one optional BYOK reranker delta.
- **LLM-as-judge answer-quality / end-task eval** (does the agent solve the task given retrieved context) — separate eval track (golden task harness `forge_eval.harness`); HARD-04 is retrieval quality only.
- **Live observability widget** surfacing recall/nDCG/reranker-delta in the web UI — owned by the Observability slice (F38); HARD-04 emits the metrics + `EVAL_REPORT.md` artifact only.
- **Multilingual / non-text corpora** and graded human-relevance judgments at scale — future; HARD-04 uses binary (with optional graded `gains`) relevance on the curated set.
- **Dim-reconciliation migration for the `vector(N)` column** to the local model's native dim on Postgres — owned by **HARD-03**; HARD-04 consumes it and otherwise runs dim-agnostic on SQLite.
- **uv re-lock of the `sentence-transformers` extra** — **HARD-14**.
