# scf-eval

Golden task set, retrieval metrics, and quality evaluation harness.

Part of the [Swarm Code Factory](../../README.md) monorepo. Apache-2.0 licensed.

## Retrieval eval — two tracks

### 1. Honest real-corpus eval (the numbers to quote) — `scf_eval.corpus_eval`

The production hybrid pipeline (semantic pgvector leg + BM25 keyword leg → RRF
k=60 → cross-encoder rerank → attributed top-k) run with a **learned local
`sentence-transformers` embedder** (`all-MiniLM-L6-v2`, 384-dim, **no API key**)
over the **real SCF monorepo** (982 files, ~14.3k chunks), scored against a
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
uv run --with sentence-transformers python -m scf_eval.corpus_eval --write-report

# The opt-in test lane (learned model; skips without the extra / the flag):
FORGE_RUN_REALEVAL=1 uv run --with sentence-transformers pytest -m realeval -q

# Optional hosted-reranker (Jina/Cohere) delta — needs a BYOK key in
# .env.integration (keys are never logged or written to the report):
FORGE_EVAL_RERANKER=jina uv run python -m scf_eval.corpus_eval
```

### 2. Deterministic wiring check (NOT a quality headline) — `scf_eval.retrieval_eval`

A synthetic 20-file corpus + 22-case golden set scored with a deterministic
feature-hashing embedder and a fixture reranker. It scores a perfect
`recall@5 = 1.000` **by construction** and exists only to prove the pipeline
wiring and ranking logic end-to-end with **no network and no model** — it is a
wiring check, not a measurement of retrieval quality. It runs in the default
hermetic suite (`uv run pytest packages/evaluation -q`).

## End-to-end benchmark — `forge-factory-bench` (`scf_eval.sweval`)

Each case is an issue, a small repository and held-out tests the solver never sees,
split into fail-to-pass and pass-to-pass. A case is **resolved** when every
fail-to-pass test passes and no pass-to-pass test fails, run in a sandbox; it is
**not resolved** otherwise, and an **infrastructure error** when it could not be
scored at all (a setup command failed, the solver or the sandbox crashed).

The suite lives in `scf_eval/benchmarks/forge-factory-bench/<version>/`:

- `manifest.yaml` lists the case files; the suite is identified by its content hash.
- `cases/<id>.yaml` holds one case: the issue text (`query`), the repository tree
  (`metadata.repo`), the held-out test files (`metadata.held_out`) and the test ids
  (`metadata.fail_to_pass`, `metadata.pass_to_pass`). The harness strips the
  held-out keys before the solver sees the case and writes the held-out files over
  the solver's change before the tests run.
- `reference.yaml` holds each case's known-good patch. Only the reference solver
  reads it.

The solver's change is untrusted input to the scorer. A change that writes pytest's
configuration or shadows pytest (any `conftest.py`, `pytest.ini`, `tox.ini`,
`setup.cfg`, `pyproject.toml`, `sitecustomize.py` or `usercustomize.py`, or a
top-level `pytest.py`, `pytest/` or `_pytest/`) is refused and scored not resolved,
with the reason in the report. A held-out test counts as passing only when pytest's
JUnit report says it ran and passed; a skipped test does not count. Candidate code
still runs inside the test process, so a change built to tamper with pytest from
within can still lie; the sandbox is the boundary for that.

Version 1.0.0 has 20 cases and is frozen;
[`docs/benchmarks/forge-factory-bench.md`](../../docs/benchmarks/forge-factory-bench.md)
describes it and its reference baseline. Run it offline with the reference solver
(every case should resolve) or the empty solver (none should):

```bash
uv run python -m scf_api.cli_bench run \
  --suite-dir packages/evaluation/scf_eval/benchmarks/forge-factory-bench/1.0.0 \
  --solver reference --out bench-out
```

The report (printed as Markdown; with `--out`, also `report.json` and `report.md`)
states the suite slug, version and content hash, the SCF commit, the solver, its
model identifiers and total cost when it reports them, the sandbox tier, each
case's outcome with the pass-to-pass tests it regressed, the resolve rate and the
wall time. The command exits non-zero when any case is an infrastructure error.
With `--out`, every finished case is stored in `results.jsonl`; rerunning the same
command skips the stored cases, so an interrupted run resumes. A stored
infrastructure error is run again; delete a case's line to rerun any other. The file
belongs to one run: results from another suite version, SCF commit, sandbox tier or
solver (another reference patch set, another instance) are refused, never reported
under this run's provenance.

Any solver works through one interface (`scf_eval.sweval.Solver`): a public case in,
the files its change writes out. `run_suite` takes the sandbox provider as a
parameter. To score a running **development** SCF instance, push each case's
repository tree to branch `bench/<case id>` of a repository connected to the
instance, then:

```python
from scf_agent.sandbox import LocalSandboxProvider
from scf_eval.sweval import ScfInstanceSolver, run_suite

solver = ScfInstanceSolver(
    base_url="https://scf.example.com/api/v1",
    token=token,
    repository="acme/bench",
    clone_dir="path/to/local/clone/of/acme/bench",
    model="the-model-id",
)
report = await run_suite(suite_dir, solver, sandbox_provider=LocalSandboxProvider())
```

The adapter submits the issue to `POST /agent/runs` on the instance's direct run
path, waits for the run to finish, and collects the change from the branch the run
pushed. A run that made no commits scores not resolved.

**Scoring a governed (production) instance is not supported yet.** Outside
development the API refuses a repository-changing run that has no task (409
`governed_run_requires_task`, `governed_run_unsupported`, `ungoverned_run_refused`),
and a governed run delivers its branch only after its workflow's checks and
approvals. The adapter records such a refusal as an infrastructure error that names
the API's reason.

`forge-swe` 1.0.0 (`scf_eval/benchmarks/forge-swe/`) is a **wiring fixture, not an
agent benchmark**: it is frozen so the leaderboard, freeze and verify paths have a
suite to run, and its agent-task cases have no repository and no tests.

## Metrics — `scf_eval.metrics`

Pure, deterministic ranked-list metrics: `recall_at_k`, `precision_at_k`,
`reciprocal_rank`, `average_precision`, `hit_at_k`, and `ndcg_at_k`
(binary or graded-gain normalised DCG). The runner (`scf_eval.runner`) scores
golden cases into a `Scorecard` with a recall + nDCG dual regression gate.
