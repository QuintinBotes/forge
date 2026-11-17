# ctxbench results

ctxbench (spec J §2.4, §2.4.1) asks whether the context Swarm Code Factory assembles for a
question holds the evidence a correct answer needs, and how large that context is. Its suite,
`ctxbench-scf` 1.0.0 (`packages/evaluation/scf_eval/benchmarks/ctxbench-scf/1.0.0/`), holds 60
questions written for SCF about Quayline, an invented berth-booking project, and the corpus they
are asked against, committed beside them: 33 repository files, 174 commits, 137 issues and 44 pull
requests, 452 chunks once ingested. No question, answer or corpus text comes from another
project's benchmark.

| Frozen by | Hash |
|---|---|
| Question set (questions and prompts) | `sha256:eeed9396c001424097b27afdbcc1105ebb3e582fb3364de2e2f20370912ffe34` |
| Corpus (`corpus.jsonl`) | `sha256:ce092fa7dcffbbb08f7ca69a7fdc1533e1764084a660bb26ef6d2d9a2357fb2d` |

## Proxy lane: a regression gate over the committed corpus

The proxy builds the corpus into a fresh BM25-only workspace (no network, no embedder, no model),
calls `assemble_context` once per question with the MCP tool's defaults (8,000 tokens, the
`default` template), and counts a hit when the bundle holds any of the question's evidence. It is
a regression gate over this corpus, not a measure of answer quality. The default test suite
(`apps/api/tests/test_cli_ctxbench.py`) runs it and gates it against the committed baseline: it
fails when a question that hit in the baseline misses, or when the p50 bundle size rises more than
10% over the baseline's. A missing retrieval leg is an error, never a score. The gate and the test
also check spec J's 17,800-token p50 ceiling, but a proxy bundle is assembled within its
8,000-token budget and can never reach it: that ceiling bounds the live lane, where an agent may
gather more context than one bundle.

The first result is committed as the baseline, `proxy-baseline.json`:

| | |
|---|---|
| Lane, embedder | proxy, none (BM25 only), on SQLite |
| Commit | `6d385c6` plus the change that adds the suite (taken before it was committed) |
| Evidence recall | **0.9643** (54 of 56 answerable questions) |
| p50 bundle size | **1,708.5 tokens** (budget 8,000) |

| Category | Recall |
|---|---|
| decision | 0.90 |
| requirement | 1.00 |
| code-location | 1.00 |
| history | 1.00 |
| tracker | 1.00 |
| learned | 0.83 |
| temporal | 1.00 |

The two misses are `scf-dec-07` (why amounts are integer pence) and `scf-learn-05` (the lesson
about a check followed by a write). The four unanswerable questions have no evidence and are not
scored.

On Postgres the result is the same 54 hits, and the same bytes on every fresh database, but three
bundles are a little larger (`scf-hist-03`, `scf-temp-03`, `scf-trk-05`; p50 1,713.5 tokens).
Postgres rescores only the 200 chunks that `ts_rank_cd` ranks first (spec J §2.5), and those
questions match more than 200 chunks, so its top 20 can differ from SQLite's full scan. The gate
passes there too.

The corpus was grown once before it was frozen. With only the project's own documents and the
issues the questions are about (135 chunks), every question hit: a perfect score by construction,
which `docs/EVAL_RESULTS.md` treats as a red flag. Routine project traffic (maintenance tickets,
small requests, dependency bumps, routine commits and a berth table) was added until the corpus
held 452 chunks. The questions were not changed.

To reproduce, against any migrated database (each run builds a fresh workspace):

```bash
FORGE_CTXBENCH_SUBJECT=example/quayline scf-cli ctxbench proxy --out proxy-result.json
scf-cli ctxbench gate proxy-result.json \
  --baseline packages/evaluation/scf_eval/benchmarks/ctxbench-scf/1.0.0/proxy-baseline.json
```

## Live lane: the baseline for #249

No live baseline exists yet: the live lane (`scf-cli ctxbench run`, `grade`, `report`) has not been
run on `ctxbench-scf` with credentials. It runs only when invoked by hand, never in CI, and refuses
without `ANTHROPIC_API_KEY`. When it is run, record here the score and the hallucination rate with
the agent and judge models, the date and the number of runs; `report` writes the full tables to the
run directory.

The pass bar for the score is not set: it is the subject of #249, which sets it from this baseline.
Hallucination is held to at most 7% and the p50 context to at most 17,800 tokens (spec J §2.4).
