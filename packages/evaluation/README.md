# forge-eval

Golden task set, retrieval metrics, and quality evaluation harness.

Part of the [Forge](../../README.md) monorepo. Apache-2.0 licensed.

## Retrieval eval — two tracks

### 1. Honest real-corpus eval (the numbers to quote) — `forge_eval.corpus_eval`

The production hybrid pipeline (semantic pgvector leg + BM25 keyword leg → RRF
k=60 → cross-encoder rerank → attributed top-k) run with a **learned local
`sentence-transformers` embedder** (`all-MiniLM-L6-v2`, 384-dim, **no API key**)
over the **real Forge monorepo** (982 files, ~14.3k chunks), scored against a
curated 36-case golden set authored *against the real repo*:

```
recall@5 = 0.778   recall@10 = 0.861   MRR = 0.597   nDCG@10 = 0.660   (36 cases)
regression gate: recall@5 >= 0.70 AND nDCG@10 >= 0.60 → PASS
```

These are **honest** numbers — not perfect by construction. A mean recall@5 of
1.000 on the real corpus is treated as a **red flag** (suspected leakage /
trivial golden set), not a pass. Full scorecard, the fusion-vs-single-leg
ablation, the regression baseline, and the Track 1.4 resolution live in
[`docs/EVAL_RESULTS.md`](../../docs/EVAL_RESULTS.md).

```bash
# First run downloads the small model to the HF cache; later runs are offline.
uv run --with sentence-transformers python -m forge_eval.corpus_eval --write-report

# The opt-in test lane (learned model; skips without the extra / the flag):
FORGE_RUN_REALEVAL=1 uv run --with sentence-transformers pytest -m realeval -q

# Optional hosted-reranker (Jina/Cohere) delta — needs a BYOK key in
# .env.integration (keys are never logged or written to the report):
FORGE_EVAL_RERANKER=jina uv run python -m forge_eval.corpus_eval
```

### 2. Deterministic wiring check (NOT a quality headline) — `forge_eval.retrieval_eval`

A synthetic 20-file corpus + 22-case golden set scored with a deterministic
feature-hashing embedder and a fixture reranker. It scores a perfect
`recall@5 = 1.000` **by construction** and exists only to prove the pipeline
wiring and ranking logic end-to-end with **no network and no model** — it is a
wiring check, not a measurement of retrieval quality. It runs in the default
hermetic suite (`uv run pytest packages/evaluation -q`).

## Metrics — `forge_eval.metrics`

Pure, deterministic ranked-list metrics: `recall_at_k`, `precision_at_k`,
`reciprocal_rank`, `average_precision`, `hit_at_k`, and `ndcg_at_k`
(binary or graded-gain normalised DCG). The runner (`forge_eval.runner`) scores
golden cases into a `Scorecard` with a recall + nDCG dual regression gate.
