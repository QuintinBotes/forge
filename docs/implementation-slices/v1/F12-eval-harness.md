# F12 — Golden Test Set & Evaluation Harness

> Phase: v1 · Spec module(s): Observability Layer (eval harness), `packages/evaluation` (`forge-eval`), Spec Engine (requirement→test traceability), Knowledge Service (retrieval metrics), Orchestrator/Agent Runtime (replayable runs) · Status target: **Done** = a versioned golden suite of ≥30 representative cases (retrieval, spec, agent_task) loads and schema-validates; the harness runs the suite against the current build, computes retrieval metrics (recall@k / precision@k / MRR / nDCG) and RAGAS metrics (context precision/recall + LLM-judge faithfulness/answer-relevancy), spec metrics (completeness, requirement & acceptance-criteria coverage from F02 traceability), and agent/workflow metrics (task-completion, requirement-satisfaction, retry/failure rate, confidence, token cost); workflow/agent runs are recorded as deterministic **replay bundles** and replayed offline with step-level inspection; a **regression gate** compares the report to a stored baseline and exits non-zero (blocking PR merge) on any gated regression; A/B comparison of two arms (e.g. rerank on/off, model provider A/B) emits a metric diff. All of it runs in CI with zero live model/network calls via fakes + cassettes.

---

## 1. Intent — what & why

The spec is explicit and repeats the point in three places: *"Evaluation is built in from day one. Golden test set ships with V1"* (Core Design Principles #10), *"Golden test set: 30-100 representative task inputs with known-good outputs — built before any framework complexity is added"* (Observability and Evaluation), and the Build Prompt's non-negotiable #10: *"A golden evaluation set of 30+ tasks must exist before framework complexity is added."* The research report reinforces this as one of the three things that make Forge buildable: *"Eval-first development … build a golden set of 30–100 representative task-and-output pairs before adding orchestration complexity"* (`docs/forge-research-report.md`, "What Makes Forge Buildable").

F12 builds the `forge-eval` package (`packages/evaluation/forge_eval`) and the harness surface that turns "quality" from an opinion into a measured, gated number. Concretely it delivers:

1. **A golden suite** — ≥30 version-controlled, schema-validated cases spanning the three things V1 actually does: hybrid retrieval (F05), spec generation/validation (F02), and plan→execute→verify→PR agent runs (F08). Cases ship as plain YAML so they are diffable, reviewable, and community-contributable (OSS extension point).
2. **Metric evaluators** — pure, testable functions for retrieval quality, RAGAS, spec quality, and agent/workflow quality, plus cost aggregation — exactly the metric families enumerated in the spec's "Key Metrics".
3. **Replayable runs** — a recorder that captures every non-deterministic interaction (model completion, tool call, retrieval result, clock/random) of a workflow/agent run into a **replay bundle**, and a player that re-executes deterministically with step-level inspection. This is what makes agent_task golden cases reproducible offline and what backs the "replayable workflow runs with step-level inspection" requirement.
4. **A regression gate** — compares an `EvalReport` to a stored baseline under declarative gate thresholds and fails CI on regression, satisfying *"Every release runs against the golden test set; regressions block merge."*
5. **A/B evaluation** — runs the suite under two arms and diffs metrics, satisfying *"A/B evaluation for retrieval strategies and model providers."*

This is deliberately the simplest eval substrate that is honest: deterministic where it can be (set-based retrieval/spec metrics, cassette replay), LLM-judged only where unavoidable (RAGAS faithfulness/answer-relevancy), and never gated on a metric that cannot be reproduced in CI.

## 2. User-facing behavior / journeys

- **Journey A — Maintainer runs the gate locally.** A contributor finishes a change to the retriever and runs `forge eval gate --suite golden`. The harness executes all golden cases against the current build using shipped replay cassettes and the deterministic fake judge, prints a metric table and a per-metric baseline comparison, and exits `0` (no regression) or `1` (e.g. `retrieval.recall_at_k dropped 0.74 → 0.69, max_regression 0.03 exceeded`). The non-zero exit blocks the merge.
- **Journey B — CI regression gate on a PR.** Opening a PR triggers the `eval-gate` GitHub Actions job (Postgres+pgvector+MinIO service containers). It seeds the synthetic fixture corpus, runs `forge eval gate`, and posts the metric diff as a PR check. A regression turns the check red and blocks merge per the spec gating rule.
- **Journey C — Inspect a failing case (step-level replay).** A maintainer opens the Eval dashboard, drills into an `agent_task` case that regressed, and views its replay timeline — each recorded model call, tool call, and retrieval step with inputs/outputs — reusing the F10 run-trace viewer. They see the agent stopped citing a different acceptance criterion than the golden expectation.
- **Journey D — Promote a new baseline.** After an intentional quality improvement, an admin runs `forge eval run --suite golden`, reviews the report, and runs `forge eval baseline set <eval_run_id>` (or clicks "Set as baseline" in the UI). Subsequent gates compare against the new numbers.
- **Journey E — A/B a retrieval strategy.** A maintainer runs `forge eval ab --arm-a default --arm-b rerank_off --suite golden` and gets a diff showing the reranker's contribution (the "reranker delta" metric): `ragas.context_recall +0.06`, `retrieval.ndcg_at_k +0.04`, winner `a`. Same mechanism A/B-tests two model providers for spec/agent cases.
- **Journey F — Contribute a golden case.** An external contributor adds `packages/evaluation/forge_eval/golden/cases/retrieval/auth-expired-token.yaml`, the loader validates it on `forge eval lint-suite`, and CI runs it.

## 3. Vertical slice

> Layout note: this slice uses the **actual** in-tree repo layout — `apps/api/forge_api` (routers in `forge_api/routers/`, registered in `routers/__init__.py::FEATURE_ROUTERS`), `apps/worker/forge_worker`, `packages/db/forge_db` (ORM in `forge_db/models/`), migrations in `packages/db/migrations/versions/`, and the eval core in `packages/evaluation/forge_eval`. The Alembic config is `packages/db/alembic.ini` and `make migrate` runs `uv run alembic -c packages/db/alembic.ini upgrade head`. The data model lives in `forge_db`, consistent with that package's stated purpose ("SQLAlchemy 2.x data model and Alembic migrations for Forge"). The repo is **metadata-driven**: the sole migration `0001_baseline.py` runs `Base.metadata.create_all`, so new tables enter the schema by registering their ORM models on `Base.metadata` (see §3.1). The retrieval interface types this slice consumes are owned by `v1/F05-hybrid-knowledge-retrieval` and live in the `forge_knowledge` package.

### 3.1 Data model (tables/columns/migrations touched)

F12 follows the repo's **metadata-driven** schema convention. The four new ORM models live in `packages/db/forge_db/models/eval.py` (`EvalRun`, `EvalCaseResult`, `EvalBaseline`, `ReplayBundle`) and are registered on `Base.metadata` via `forge_db/models/__init__.py`; because the baseline migration `packages/db/migrations/versions/0001_baseline.py` runs `Base.metadata.create_all`, both fresh production installs and the SQLite unit suite create the tables automatically. All indexes — including the partial unique active-baseline index — are declared in each model's `__table_args__` using dialect-portable constructs (`Index(..., postgresql_where=…, sqlite_where=…)`; both Postgres and SQLite support partial indexes) so `create_all` emits them. **While `0001_baseline.py` remains the sole migration, F12 adds NO chained `op.create_table` migration** — that would double-create the tables `create_all` already makes and break `alembic upgrade head` (and `test_migration.py`'s single-base/single-linear-head asserts). Registering the models is the only schema change. (The identical DDL would ship as an incremental `packages/db/migrations/versions/00NN_eval_harness.py` only once the baseline is later frozen post-release, guarded with an inspector `has_table` check so it no-ops where the tables already exist, `down_revision` set to the then-current head; `downgrade` drops the four tables.)

**Required test update (load-bearing):** `packages/db/tests/test_models.py::EXPECTED_MODELS` and `packages/db/tests/test_migration.py::EXPECTED_TABLES` assert the model/table set is **exactly** the spec set (21 today). F12 extends both: add `EvalRun`/`EvalCaseResult`/`EvalBaseline`/`ReplayBundle` to `EXPECTED_MODELS` and the corresponding `eval_run`/`eval_case_result`/`eval_baseline`/`replay_bundle` to `EXPECTED_TABLES`, and extend the `__all__` export in `models/__init__.py`.

The golden suite itself is **file-based** (canonical source = YAML in the package); the DB stores run results, baselines, and the replay-bundle index only.

**Table `eval_run`**

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | `gen_random_uuid()` |
| `workspace_id` | `uuid` NULL FK → `workspace.id` ON DELETE CASCADE | NULL = system-level CI run (no tenant) |
| `suite` | `text` NOT NULL | e.g. `golden` |
| `arm_label` | `text` NOT NULL DEFAULT `'default'` | A/B arm identity |
| `config` | `jsonb` NOT NULL DEFAULT `'{}'` | serialized `ArmConfig` |
| `git_sha` | `text` NULL | build under test |
| `status` | `text` NOT NULL DEFAULT `'running'` | `running \| succeeded \| failed \| error` |
| `total` / `passed` / `failed` / `errored` | `integer` NOT NULL DEFAULT 0 | case counts |
| `aggregate` | `jsonb` NOT NULL DEFAULT `'{}'` | `{metric_name: MetricSummary}` |
| `regression_passed` | `boolean` NULL | NULL until gate evaluated |
| `baseline_eval_run_id` | `uuid` NULL FK → `eval_run.id` | baseline compared against |
| `report_object_key` | `text` NULL | MinIO key for full JSON + markdown report |
| `created_by` | `uuid` NULL FK → `app_user.id` | NULL for CI/system |
| `started_at` / `finished_at` | `timestamptz` | |

Indexes: btree `(suite, arm_label, started_at DESC)`; btree `(workspace_id, started_at DESC)`.

**Table `eval_case_result`**

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `eval_run_id` | `uuid` NOT NULL FK → `eval_run.id` ON DELETE CASCADE | |
| `case_id` | `text` NOT NULL | golden case slug |
| `case_type` | `text` NOT NULL | `retrieval \| spec \| agent_task` |
| `status` | `text` NOT NULL | `passed \| failed \| error \| skipped` |
| `score` | `numeric(5,4)` NOT NULL | normalized 0..1 primary score for gating |
| `metrics` | `jsonb` NOT NULL DEFAULT `'{}'` | flat `{dotted_metric: value}` map |
| `expected` | `jsonb` NOT NULL DEFAULT `'{}'` | golden expectation snapshot (audit) |
| `actual` | `jsonb` NOT NULL DEFAULT `'{}'` | what the system produced (truncated) |
| `replay_bundle_id` | `uuid` NULL FK → `replay_bundle.id` | for agent_task cases |
| `duration_ms` | `integer` NOT NULL | |
| `error` | `text` NULL | |
| `created_at` | `timestamptz` NOT NULL DEFAULT now() | |

Indexes: `UNIQUE (eval_run_id, case_id)`; btree `(case_id, created_at DESC)` (per-case trend).

**Table `eval_baseline`**

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `suite` | `text` NOT NULL | |
| `arm_label` | `text` NOT NULL DEFAULT `'default'` | |
| `eval_run_id` | `uuid` NOT NULL FK → `eval_run.id` | the run promoted to baseline |
| `metrics` | `jsonb` NOT NULL | frozen `{dotted_metric: value}` snapshot used by the gate |
| `active` | `boolean` NOT NULL DEFAULT true | |
| `set_by` | `uuid` NULL FK → `app_user.id` | |
| `set_at` | `timestamptz` NOT NULL DEFAULT now() | |

Partial unique index: one active baseline per (suite, arm) — `CREATE UNIQUE INDEX uq_active_baseline ON eval_baseline (suite, arm_label) WHERE active;`.

**Table `replay_bundle`** (recorded cassette index; payload in MinIO)

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `workspace_id` | `uuid` NULL FK → `workspace.id` ON DELETE CASCADE | |
| `kind` | `text` NOT NULL | `workflow \| agent \| retrieval` |
| `workflow_run_id` | `uuid` NULL FK → `workflow_run.id` | source run, if live-recorded |
| `agent_run_id` | `uuid` NULL FK → `agent_run.id` | |
| `event_count` | `integer` NOT NULL | |
| `content_hash` | `text` NOT NULL | sha256 of canonicalized event list — dedup + determinism check |
| `schema_version` | `integer` NOT NULL | bundle format version |
| `object_key` | `text` NOT NULL | MinIO key, `eval/replay/{ws|system}/{id}.json.zst` |
| `recorded_at` | `timestamptz` NOT NULL DEFAULT now() | |

Index: btree `(kind, recorded_at DESC)`; btree `(content_hash)`.

`alembic downgrade` drops all four tables cleanly.

### 3.2 Backend (FastAPI routes + services/packages)

New router `apps/api/forge_api/routers/eval.py` (declares `APIRouter(prefix="/eval", tags=["eval"])` and is added to `FEATURE_ROUTERS` in `apps/api/forge_api/routers/__init__.py`, so it mounts under `Settings.api_prefix` — e.g. `/api/v1/eval`). All routes authenticated via the foundation auth dependency + workspace-scoped. RBAC: `require_role("admin")` to trigger runs and set baselines; `member`/`agent-runner` read-only (`require_role("member")`). Thin controllers delegate to `apps/api/forge_api/services/eval_service.py`, which wires `forge_eval` to the async SQLAlchemy session (`forge_api.db`), the MinIO artifact store, the `cross-cutting/F37-auth-secrets-byok` key vault (judge model keys), the F05 retriever, the F02 spec/validation readers, and Celery.

| Method | Path | RBAC | Purpose |
|---|---|---|---|
| `POST` | `/runs` | admin | Enqueue an eval run (`StartEvalRequest` → `{eval_run_id}`); Celery. |
| `GET` | `/runs` | member | List runs (filter by suite/arm/status), with aggregate metrics. |
| `GET` | `/runs/{eval_run_id}` | member | Run detail: `EvalReport` aggregate + per-case results + regression result. |
| `GET` | `/runs/{eval_run_id}/cases/{case_id}` | member | Case detail incl. expected/actual + `replay_bundle_id`. |
| `POST` | `/runs/{eval_run_id}/gate` | member | Evaluate regression gate vs active baseline → `RegressionResult`. |
| `POST` | `/baselines` | admin | Promote a run to baseline (`{suite, arm_label, eval_run_id}`). |
| `GET` | `/baselines` | member | Active baselines per suite/arm. |
| `POST` | `/ab` | admin | Run/compare two arms → `ABComparison` (enqueues two runs). |
| `GET` | `/replay/{bundle_id}` | member | Replay manifest + per-step events (signed-URL for raw payload); feeds the step-level inspector. |

`packages/evaluation/forge_eval/` (framework-agnostic core; no FastAPI/SQLAlchemy leakage in pure layers):

```
packages/evaluation/forge_eval/
├── __init__.py
├── models.py             # Pydantic v2 models + enums (§4)
├── protocols.py          # GoldenLoader, LLMJudge, EvalTarget, Replayer, EvalRunRepository (§4)
├── golden/
│   ├── loader.py         # YAMLGoldenLoader: load + schema-validate + cross-check case_type
│   ├── gates.yaml        # default regression gate thresholds (§4)
│   └── cases/            # the ≥30 golden YAML cases, grouped by type
│       ├── retrieval/    # ≥12 cases
│       ├── spec/         # ≥8 cases
│       └── agent_task/   # ≥10 cases (+ shipped replay cassettes)
├── metrics/
│   ├── retrieval.py      # recall_at_k, precision_at_k, mrr, ndcg_at_k, reranker_delta
│   ├── ragas.py          # context_precision, context_recall (deterministic) + faithfulness/answer_relevancy (LLMJudge)
│   ├── spec.py           # completeness, requirement_coverage, acceptance_criteria_coverage
│   ├── workflow.py       # task_completion, requirement_satisfaction, retry_rate, failure_rate, confidence stats
│   ├── cost.py           # token_cost aggregation from recorded usage
│   └── aggregate.py      # summarize(list[float]) -> MetricSummary (mean/p50/p95/count)
├── replay/
│   ├── recorder.py       # ReplayRecorder: wrap model/tool/retrieval seams, emit ReplayBundle
│   ├── player.py         # ReplayPlayer: deterministic playback; ReplayDivergenceError on unmatched key
│   └── store.py          # bundle (de)serialize (zstd json) + MinIO get/put + content_hash
├── targets/
│   ├── retrieval_target.py  # drives F05 HybridRetrievalService for a retrieval case
│   ├── spec_target.py       # drives F02 spec generator + validation reader for a spec case
│   └── agent_target.py      # replays an agent_task cassette through F08 flow, grades result
├── harness/
│   ├── runner.py         # EvalRunner.run(...) -> EvalReport
│   ├── compare.py        # compare_to_baseline(...) -> RegressionResult; load_gates()
│   └── ab.py             # ab_compare(report_a, report_b) -> ABComparison
└── report.py             # render EvalReport -> markdown (CI summary) + json
```

CLI at `apps/api/forge_api/cli/eval.py`, registered as the `eval` sub-command group on the project console script (the same entrypoint the spec/deploy docs invoke as `forge-cli`, e.g. `docker compose exec api forge-cli eval gate`; the shorthand `forge eval …` below is that console script). Matches the sibling convention in `v1/F04-repo-policy` (`apps/api/forge_api/cli/policy.py`):
- `forge eval run --suite golden [--arm default] [--git-sha SHA] [--out report.md]`
- `forge eval gate --suite golden [--arm default]` (exit `1` on regression — the merge-blocking command)
- `forge eval baseline set <eval_run_id>` / `forge eval baseline show --suite golden`
- `forge eval ab --suite golden --arm-a default --arm-b rerank_off`
- `forge eval lint-suite --suite golden` (validate all golden YAML; exit `1` on schema error or `<30` cases)

### 3.3 Worker / agent runtime (Celery tasks, LangGraph, if any)

Celery tasks in `apps/worker/forge_worker/tasks/eval.py` (queue `eval`):

- `eval.run_suite(eval_run_id: str) -> dict` — load golden cases, build `ArmConfig` from the `eval_run.config`, construct the per-type `EvalTarget`s (DI: F05 retriever, F02 readers, F08 replay player, judge), run cases with bounded concurrency, write `eval_case_result` rows, compute `aggregate`, upload the report to MinIO, set `eval_run.status`, then evaluate the regression gate against the active baseline and set `regression_passed`.
- `eval.run_case(eval_run_id: str, case_id: str) -> dict` — single-case execution (used for fan-out / re-run of one case).
- `eval.run_ab(suite, arm_a, arm_b, workspace_id) -> dict` — enqueues two `run_suite` runs and persists an `ABComparison` artifact when both complete.
- `eval.persist_replay_bundle(kind, run_id, events_json) -> dict` — called by the agent runtime/orchestrator after a real run to store a `ReplayBundle` (recorder output) in MinIO + index row. This is the hook that makes live runs replayable.

**Recorder integration (the only agent-runtime touch point):** F06's agent loop and F08's orchestrator obtain their model client, tool registry, and retriever through DI seams. F12 ships `ReplayRecorder` as decorators over those seams; when `FORGE_RECORD_REPLAY=true` (or a per-run flag) is set, every model/tool/retrieval call is appended to the bundle. No LangGraph graph is added here — F12 wraps existing seams and replays them. `agent_target` runs a golden `agent_task` case by feeding `ReplayPlayer` to F08's `start_agent_run`/`run_checks` path so the agent produces a deterministic `AgentRunResult` with no network.

Determinism guards: replay matches each call by a stable `key` (e.g. `sha256(model + canonical_prompt)` / `sha256(tool + canonical_args)`); an unmatched call raises `ReplayDivergenceError` (surfaced as a `failed` case with reason) so code drift that changes the call sequence is caught rather than silently mocked.

### 3.4 Frontend / UI (Next.js routes/components, if any)

Minimal V1 dashboard (the rich trace UI is owned by F10 and reused):

- `apps/web/app/(app)/eval/page.tsx` — runs list with suite/arm filter, status, regression badge (green/red), and a sparkline of the primary score per suite over time. TanStack Query against `/runs`.
- `apps/web/app/(app)/eval/[runId]/page.tsx` — run detail: aggregate metric table, regression findings (baseline vs current vs delta, violated rows highlighted), per-case TanStack Table, and a "Set as baseline" action (admin).
- `apps/web/components/eval/CaseReplayDrawer.tsx` — opens a case's `replay_bundle_id` and renders the step timeline by **reusing F10 `RunTraceTimeline`** (model/tool/retrieval steps with inputs/outputs).
- `apps/web/lib/api/eval.ts` — typed client matching §4 contracts.

### 3.5 Infra / deploy (compose, helm, caddy, if any)

- **No new compose service.** Eval runs on the existing `worker` (new `eval` queue) and stores artifacts/cassettes in the existing `minio`. Retrieval cases reuse the F05 `db` (pgvector) and `reranker` services.
- **CI workflow** `.github/workflows/eval-gate.yml` — on `pull_request`: spins up `postgres:pgvector`, `redis`, `minio` service containers, `uv sync`, `alembic upgrade head`, seeds the synthetic fixture corpus, then `uv run forge eval gate --suite golden`. Job fails (blocking merge) on non-zero exit; uploads the markdown report as an artifact and posts it as a PR comment. Runs with `EVAL_LLM_JUDGE_ENABLED=false` (judge metrics reported-but-not-gated in CI) so no model key is required.
- **Env vars** (add to `deploy/.env.example`, `.env.production.example`): `EVAL_GOLDEN_DIR` (default packaged path), `EVAL_LLM_JUDGE_ENABLED` (default `false`), `EVAL_JUDGE_MODEL` (e.g. provider/model id; BYOK), `EVAL_REGRESSION_DEFAULT_TOLERANCE` (default `0.03`), `EVAL_BASELINE_REQUIRED` (default `false` — first run with no baseline passes the gate and is auto-promotable), `EVAL_CONCURRENCY` (default `4`), `FORGE_RECORD_REPLAY` (default `false`).

## 4. Public interfaces / contracts (exact signatures, Pydantic models, Protocols, YAML schemas)

`packages/evaluation/forge_eval/models.py`:

```python
from __future__ import annotations
from datetime import datetime
from enum import StrEnum
from uuid import UUID
from pydantic import BaseModel, Field, model_validator

SCHEMA_VERSION = 1

class CaseType(StrEnum):
    retrieval = "retrieval"; spec = "spec"; agent_task = "agent_task"

class CaseStatus(StrEnum):
    passed = "passed"; failed = "failed"; error = "error"; skipped = "skipped"

# ---- Golden case expectations -------------------------------------------------
class GradedDoc(BaseModel):
    source_uri: str                      # matches forge_knowledge RankedChunk.source_uri (F05)
    relevance: int = 1                   # graded relevance for nDCG (>=1 == relevant)

class RetrievalExpectation(BaseModel):
    query: str
    scope: dict = Field(default_factory=dict)     # serialized forge_knowledge.KnowledgeScope (F05)
    relevant: list[GradedDoc]
    reference_answer: str | None = None           # enables RAGAS faithfulness/answer_relevancy

class SpecExpectation(BaseModel):
    brief: str
    repos: list[str] = Field(default_factory=list)
    expected_requirements: list[str]              # normalized requirement statements
    expected_acceptance_criteria: list[str]

class AgentTaskExpectation(BaseModel):
    task_ref: str
    repo_fixture: str                             # synthetic seeded repo id/ref
    spec_ref: str
    expected_satisfied_criteria: list[str]        # e.g. ["SPEC-17/A1","SPEC-17/A2"]
    expected_changed_files: list[str]
    expected_terminal_state: str                  # e.g. "pr_opened"
    min_confidence: float = 0.0
    replay_bundle_key: str | None = None          # shipped cassette MinIO/relative key

class GoldenCase(BaseModel):
    id: str
    suite: str = "golden"
    case_type: CaseType
    title: str
    tags: list[str] = Field(default_factory=list)
    weight: float = 1.0
    k: int = 8                                     # cutoff for @k metrics
    schema_version: int = SCHEMA_VERSION
    retrieval: RetrievalExpectation | None = None
    spec: SpecExpectation | None = None
    agent_task: AgentTaskExpectation | None = None

    @model_validator(mode="after")
    def _exactly_one_payload(self) -> "GoldenCase":
        present = [p for p in (self.retrieval, self.spec, self.agent_task) if p is not None]
        if len(present) != 1:
            raise ValueError("exactly one expectation payload required")
        expected = {CaseType.retrieval: self.retrieval, CaseType.spec: self.spec,
                    CaseType.agent_task: self.agent_task}[self.case_type]
        if expected is None:
            raise ValueError(f"payload must match case_type={self.case_type}")
        return self

# ---- Metrics ------------------------------------------------------------------
class RetrievalMetrics(BaseModel):
    k: int; recall_at_k: float; precision_at_k: float; mrr: float; ndcg_at_k: float

class RagasScores(BaseModel):
    context_precision: float
    context_recall: float
    faithfulness: float | None = None             # None when judge disabled
    answer_relevancy: float | None = None

class SpecMetrics(BaseModel):
    completeness: float
    requirement_coverage: float
    acceptance_criteria_coverage: float

class AgentMetrics(BaseModel):
    completed: bool
    reached_expected_state: bool
    requirement_satisfaction_rate: float
    confidence: float
    retries: int
    token_cost_usd: float | None = None

class CaseResult(BaseModel):
    case_id: str
    case_type: CaseType
    status: CaseStatus
    score: float                                  # normalized 0..1 primary, used by the gate
    metrics: dict[str, float] = Field(default_factory=dict)  # flat dotted map
    retrieval: RetrievalMetrics | None = None
    ragas: RagasScores | None = None
    spec: SpecMetrics | None = None
    agent: AgentMetrics | None = None
    replay_bundle_id: UUID | None = None
    duration_ms: int
    error: str | None = None

class MetricSummary(BaseModel):
    name: str; mean: float; p50: float; p95: float; count: int

class EvalReport(BaseModel):
    eval_run_id: UUID
    suite: str
    arm_label: str
    git_sha: str | None = None
    config: dict = Field(default_factory=dict)
    total: int; passed: int; failed: int; errored: int
    aggregate: dict[str, MetricSummary]
    results: list[CaseResult]
    started_at: datetime
    finished_at: datetime

# ---- Arms / gates / A-B -------------------------------------------------------
class ArmConfig(BaseModel):
    label: str = "default"
    rerank: bool = True
    retrieval_top_k: int = 8
    rrf_k: int = 60
    embedding_model: str | None = None
    model_provider: str | None = None             # for spec/agent targets (BYOK)
    judge_model: str | None = None
    judge_enabled: bool = False
    extra: dict = Field(default_factory=dict)

class MetricGate(BaseModel):
    metric: str                                   # dotted, e.g. "retrieval.recall_at_k"
    min: float | None = None                      # absolute floor
    max_regression: float = 0.03                  # allowed worsening vs baseline
    direction: str = "higher_is_better"           # higher_is_better | lower_is_better

class RegressionFinding(BaseModel):
    metric: str; baseline: float | None; current: float
    delta: float; violated: bool; reason: str

class RegressionResult(BaseModel):
    passed: bool
    suite: str
    arm_label: str
    findings: list[RegressionFinding]

class ABComparison(BaseModel):
    suite: str; arm_a: str; arm_b: str
    deltas: dict[str, float]                       # metric -> (mean_b - mean_a)
    winner: dict[str, str]                         # metric -> "a" | "b" | "tie"

# ---- Replay -------------------------------------------------------------------
class ReplayEventKind(StrEnum):
    model_call = "model_call"; tool_call = "tool_call"
    retrieval = "retrieval"; clock = "clock"; random = "random"

class ReplayEvent(BaseModel):
    seq: int
    kind: ReplayEventKind
    key: str                                       # deterministic match key
    request_hash: str
    response: dict                                 # recorded output payload
    usage: dict = Field(default_factory=dict)      # {"tokens_in":..,"tokens_out":..,"cost_usd":..}
    ts_offset_ms: int

class ReplayBundle(BaseModel):
    id: UUID
    kind: str                                      # "workflow" | "agent" | "retrieval"
    workflow_run_id: UUID | None = None
    agent_run_id: UUID | None = None
    schema_version: int = SCHEMA_VERSION
    events: list[ReplayEvent]
    content_hash: str
    recorded_at: datetime

class ReplayResult(BaseModel):
    """Outcome of replaying a bundle: the deterministic outputs the player
    produced plus integrity/usage rollups the targets grade against."""
    bundle_id: UUID
    matched_events: int
    total_events: int
    diverged: bool = False                          # True if a ReplayDivergenceError was caught
    divergence_reason: str | None = None
    agent_result: dict | None = None               # serialized F06 AgentRunResult (agent_task)
    usage_total: dict = Field(default_factory=dict) # summed {"tokens_in","tokens_out","cost_usd"}
    content_hash: str                               # echoes the replayed bundle's hash
```

`packages/evaluation/forge_eval/protocols.py`:

```python
from typing import Protocol, Mapping, Sequence
from uuid import UUID
from .models import (GoldenCase, CaseResult, ArmConfig, EvalReport,
                     ReplayBundle, ReplayResult, CaseType)

class GoldenLoader(Protocol):
    def load(self, suite: str) -> list[GoldenCase]: ...          # validated, sorted by id

class LLMJudge(Protocol):
    name: str
    async def score_faithfulness(self, *, answer: str, contexts: Sequence[str]) -> float: ...
    async def score_answer_relevancy(self, *, question: str, answer: str) -> float: ...

class EvalTarget(Protocol):
    case_type: CaseType
    async def run_case(self, case: GoldenCase, *, arm: ArmConfig) -> CaseResult: ...

class Replayer(Protocol):
    async def play(self, bundle: ReplayBundle) -> ReplayResult: ...

class EvalRunRepository(Protocol):
    async def create_run(self, *, suite: str, arm: ArmConfig, git_sha: str | None,
                         workspace_id: UUID | None) -> UUID: ...
    async def save_result(self, eval_run_id: UUID, result: CaseResult) -> None: ...
    async def finalize(self, eval_run_id: UUID, report: EvalReport,
                       report_object_key: str) -> None: ...
    async def active_baseline_metrics(self, suite: str, arm_label: str) -> Mapping[str, float] | None: ...
```

`packages/evaluation/forge_eval/metrics/retrieval.py` (load-bearing pure functions):

```python
def recall_at_k(retrieved: Sequence[str], relevant: set[str], k: int) -> float:
    """|retrieved[:k] ∩ relevant| / |relevant|; 0.0 if relevant is empty."""

def precision_at_k(retrieved: Sequence[str], relevant: set[str], k: int) -> float:
    """|retrieved[:k] ∩ relevant| / k."""

def mrr(retrieved: Sequence[str], relevant: set[str]) -> float:
    """1 / (1-based rank of first relevant); 0.0 if none."""

def ndcg_at_k(retrieved: Sequence[str], graded: Mapping[str, int], k: int) -> float:
    """DCG = Σ rel_i / log2(i+1) over retrieved[:k]; normalized by ideal DCG."""

def reranker_delta(with_rerank: float, without_rerank: float) -> float:
    """Metric difference attributable to reranking (A/B)."""
```

`packages/evaluation/forge_eval/harness/runner.py` & `compare.py`:

```python
class EvalRunner:
    def __init__(self, loader: GoldenLoader,
                 targets: Mapping[CaseType, EvalTarget],
                 repo: EvalRunRepository,
                 judge: LLMJudge | None = None): ...
    async def run(self, *, suite: str, arm: ArmConfig, git_sha: str | None = None,
                  workspace_id: UUID | None = None, concurrency: int = 4) -> EvalReport: ...

def load_gates(suite: str) -> list[MetricGate]: ...   # from golden/gates.yaml

def compare_to_baseline(report: EvalReport,
                        baseline: Mapping[str, float] | None,
                        gates: Sequence[MetricGate]) -> RegressionResult:
    """For each gate, `current = report.aggregate[gate.metric].mean` (the
    suite-level mean of that dotted metric across cases; missing key => the
    gate is reported as `current=NaN, violated=True, reason="metric absent"`).
    Violated if `current < min` (for `higher_is_better`; inverted for
    `lower_is_better`), OR if it worsens vs `baseline[gate.metric]` by more
    than `max_regression` (direction-aware). Missing baseline => only absolute
    `min` gates apply (passes if none). `baseline` is the frozen
    `eval_baseline.metrics` snapshot, whose values are baseline aggregate
    means keyed by the same dotted names."""

def ab_compare(report_a: EvalReport, report_b: EvalReport) -> ABComparison: ...
```

**Canonical flat metric keys (load-bearing — gate `metric` strings, `CaseResult.metrics` keys, and `EvalReport.aggregate` keys are the SAME dotted namespace).** Each target writes these into `CaseResult.metrics` per case; `aggregate.summarize` produces one `MetricSummary` per key (mean is the suite-level value the gate reads). Rate-style metrics are emitted per case as `1.0`/`0.0` so their mean equals the rate:

| Key | Per-case value | Cases |
|---|---|---|
| `retrieval.recall_at_k` / `retrieval.precision_at_k` / `retrieval.mrr` / `retrieval.ndcg_at_k` | from `RetrievalMetrics` | retrieval |
| `ragas.context_precision` / `ragas.context_recall` | deterministic from graded ground truth | retrieval |
| `ragas.faithfulness` / `ragas.answer_relevancy` | LLM-judged; **omitted** (no key) when judge disabled | retrieval |
| `spec.completeness` / `spec.requirement_coverage` / `spec.acceptance_criteria_coverage` | from `SpecMetrics` | spec |
| `agent.task_completion_rate` | `1.0` if `AgentMetrics.completed and reached_expected_state` else `0.0` | agent_task |
| `agent.requirement_satisfaction_rate` | `AgentMetrics.requirement_satisfaction_rate` | agent_task |
| `agent.retry_rate` | `1.0` if `retries > 0` else `0.0` | agent_task |
| `agent.failure_rate` | `1.0` if `status != passed` else `0.0` | agent_task |
| `cost.token_cost_usd` | `AgentMetrics.token_cost_usd or 0.0` (reported, not gated) | agent_task |

FastAPI request schemas (`apps/api/forge_api/schemas/eval.py`):

```python
class StartEvalRequest(BaseModel):
    suite: str = "golden"
    arm: ArmConfig = ArmConfig()
    git_sha: str | None = None

class SetBaselineRequest(BaseModel):
    suite: str
    arm_label: str = "default"
    eval_run_id: UUID

class StartABRequest(BaseModel):
    suite: str = "golden"
    arm_a: ArmConfig
    arm_b: ArmConfig
# Responses reuse forge_eval.models (EvalReport, RegressionResult, ABComparison).
```

Golden case YAML schema (retrieval example, `golden/cases/retrieval/auth-expired-token.yaml`):

```yaml
id: retrieval-auth-expired-token
suite: golden
case_type: retrieval
title: "Find where expired bearer tokens are rejected"
tags: [retrieval, code, exact-symbol]
k: 8
retrieval:
  query: "where do we reject an expired bearer token"
  scope:
    repos: ["fixtures/repo-api"]
    source_types: ["repo"]
  reference_answer: "auth middleware rejects expired tokens with 401 in reject_expired_token"
  relevant:
    - { source_uri: "repo://fixtures/repo-api@HEAD/app/middleware/auth.py#L10-L42", relevance: 3 }
    - { source_uri: "repo://fixtures/repo-api@HEAD/tests/test_auth.py#L1-L30", relevance: 1 }
```

Default gates (`golden/gates.yaml`):

```yaml
suite: golden
gates:
  - { metric: retrieval.recall_at_k,                 min: 0.70, max_regression: 0.03 }
  - { metric: retrieval.ndcg_at_k,                              max_regression: 0.03 }
  - { metric: ragas.context_recall,                            max_regression: 0.03 }
  - { metric: spec.completeness,                     min: 0.80, max_regression: 0.05 }
  - { metric: spec.acceptance_criteria_coverage,     min: 0.80, max_regression: 0.05 }
  - { metric: agent.requirement_satisfaction_rate,   min: 0.80, max_regression: 0.05 }
  - { metric: agent.task_completion_rate,            min: 0.90, max_regression: 0.05 }
# NOTE: ragas.faithfulness / answer_relevancy are LLM-judged -> reported, never gated in CI.
```

## 5. Dependencies — features/slices that must exist first

Referenced by `<phase>/<id>-<slug>` path matching the files in `docs/implementation-slices/`. **Slug reconciliation:** the platform scaffold is a cross-cutting Phase-0 prerequisite every v1 slice assumes; it has no single dedicated file yet and sibling slices name it variously as `v1/F00-foundation-substrate`, `cross-cutting/F00-platform-foundation`, and `cross-cutting/C01-monorepo-and-api-foundations`. `v1/F00-foundation-substrate` is used here as the authoritative placeholder (the most common across siblings); reconcile to the final foundation slug when it lands. All numbered v1 and `cross-cutting/F3x` slugs below match real files.

- **v1/F00-foundation-substrate** (hard, placeholder) — uv workspace; `apps/api` FastAPI skeleton (`forge_api`, `routers/__init__.py::FEATURE_ROUTERS`, `Settings.api_prefix`, the auth dependency); async SQLAlchemy 2.x session + shared `Base`/`TimestampMixin`/`WorkspaceScopedModel` in `packages/db` (`forge_db`); the `0001_baseline.py` Alembic baseline + `packages/db/alembic.ini`; `apps/worker` Celery app + queues; the `forge-cli` console script; and the MinIO `ArtifactStore` (+ signed URLs) used for reports and replay bundles.
- **cross-cutting/F37-auth-secrets-byok** (hard) — the auth `Principal` + `require_role(...)` RBAC dependency (`admin`/`member`/`viewer`/`agent-runner`), the encrypted per-workspace **BYOK** vault that resolves `EVAL_JUDGE_MODEL` credentials at call time (never logged/persisted), and the canonical `SecretRedactor` reused alongside F05's `redact_secrets` for replay/report redaction (§8, AC14).
- **cross-cutting/F39-audit-log** (hard) — the central immutable `AuditEvent`/`AuditSink` (in `packages/contracts`) + `SqlAuditWriter`; F12 emits `eval.run_started`, `eval.baseline_set`, `eval.ab_started`, and `eval.replay_fetched` audit events (actor, suite, arm, `eval_run_id`) satisfying the spec's immutable-audit requirement (§8, AC20). Degrades to local structured logging if the audit slice lags.
- **v1/F05-hybrid-knowledge-retrieval** (hard) — the `retrieval` target grades `HybridRetrievalService`; F12 reuses `forge_knowledge` (F05) `KnowledgeScope`, `RankedChunk.source_uri` (the metric match key), the seed corpus fixtures, and `redact_secrets`. Provides `RETRIEVAL_TOP_K` / `RRF_K` / `RERANK_ENABLED` arm knobs. Retrieval (incl. any MCP query-through via F09) is **read-only**, honoring the MCP read-only-default non-negotiable.
- **v1/F02-spec-engine** (hard) — the `spec` target uses the spec generator (deterministic template generator usable offline) and consumes the `ValidationReport` / requirement→test traceability the spec calls out as a harness input.
- **v1/F08-plan-execute-verify-pr-approval** (hard) — provides `VerificationReport` (in `packages/contracts/forge_contracts/verification.py`) and the `run_checks` / `open_pr_with_spec_traceability` gate flow the `agent_task` target replays and grades. The `WorkflowRun`/`AgentRun` tables come from the foundation baseline; `AgentRunResult` is the **F06**-frozen contract (`forge_contracts`) consumed by both F08 and F12.
- **v1/F06-single-execution-agent** (soft) — provides the model client, tool registry, and retriever DI seams the `ReplayRecorder` decorates and the `ReplayPlayer` drives, plus the frozen `AgentRunResult` shape the `agent_target` grades. Absent → `agent_task` cases run only from shipped pre-recorded cassettes (no live recording).
- **v1/F07-feature-workflow-fsm** (soft) — provides `workflow_run` + `workflow_transition` and the FSM whose transitions are recorded/replayed; needed only for end-to-end live recording, not for cassette replay.
- **v1/F10-run-trace-viewer** (soft) — the case replay drawer reuses `RunTraceTimeline`; F12 ships a minimal fallback list if F10 lags.
- **cross-cutting/F38-observability-cost-metrics** (soft, consumer-of) — owns *live/production* metric collection (MCP freshness lag, retrieval latency p50/p95/p99, PR acceptance rate, MTTM/MTTC); F12 is the **offline golden-suite** complement and does **not** compute those live metrics (see §12). F12's per-case `token_cost_usd` reuses F38's model-pricing table where present.

## 6. Acceptance criteria (numbered, testable)

1. Registering the four ORM models on `Base.metadata` makes `Base.metadata.create_all` (the baseline upgrade path + the SQLite unit suite) produce `eval_run`, `eval_case_result`, `eval_baseline`, `replay_bundle` with the documented indexes and the partial unique active-baseline index (asserted via inspector index/table existence); `alembic upgrade head` then `downgrade base` round-trips cleanly with no double-create; and `packages/db/tests/test_models.py::EXPECTED_MODELS` + `test_migration.py::EXPECTED_TABLES` (which assert the set is **exactly** the spec models) are extended with the four new models/tables and stay green.
2. `YAMLGoldenLoader.load("golden")` loads **≥30** cases, all schema-valid, with unique ids, and the `exactly_one_payload` validator rejects a case whose payload does not match its `case_type`; `forge eval lint-suite` exits `1` if either invariant breaks.
3. Retrieval metrics are exact against hand-computed values: for `retrieved=[a,b,c,d]`, `relevant={b,d}`, `k=3` → `recall_at_k=0.5`, `precision_at_k≈0.333`, `mrr=0.5`; `ndcg_at_k` matches the graded-DCG/ideal-DCG hand calculation; empty `relevant` yields `recall=0.0` without ZeroDivisionError.
4. RAGAS `context_precision` and `context_recall` are computed deterministically from graded ground truth (no LLM); `faithfulness`/`answer_relevancy` are computed only when `judge_enabled` and a `reference_answer` exist, and are `None` otherwise.
5. The `retrieval` target runs an end-to-end case against the seeded F05 retriever and produces a `RetrievalMetrics` whose `source_uri` matching uses `RankedChunk.source_uri`; a known case yields `recall_at_k ≥ 0.5`.
6. `agent_task` replay is deterministic: replaying the same cassette twice yields byte-identical `metrics` and the same `content_hash`; a cassette whose recorded call sequence no longer matches the code path raises `ReplayDivergenceError`, surfaced as a `failed` `CaseResult` with a `reason`, **not** a silent pass.
7. `compare_to_baseline` fails the gate when a gated metric drops below `min` OR worsens versus baseline by more than `max_regression` (direction-aware), and passes when within tolerance; `lower_is_better` metrics invert the comparison. Verified with table-driven cases.
8. With no active baseline and `EVAL_BASELINE_REQUIRED=false`, only absolute `min` gates apply; the run passes if all `min` gates pass and is promotable to baseline.
9. `forge eval gate --suite golden` exits `0` on no regression and `1` on regression; the non-zero exit is what the CI `eval-gate` job uses to block merge (asserted via subprocess in an integration test).
10. `eval.run_suite` persists one `eval_case_result` per case, an `eval_run` with populated `aggregate` (`MetricSummary` per metric with mean/p50/p95/count), uploads the report to MinIO, and sets `regression_passed`.
11. `POST /baselines` (admin) promotes a run and atomically deactivates the prior active baseline for that (suite, arm); a second active baseline for the same key is rejected by `uq_active_baseline`.
12. `ab_compare` over two arms (`rerank` on vs off) returns per-metric `deltas` and `winner`, and `reranker_delta` equals `mean(arm_a.metric) - mean(arm_b.metric)` for the gated retrieval metric.
13. RBAC: `POST /runs`, `POST /baselines`, `POST /ab` require `admin`; `GET` routes require workspace membership; `agent-runner` can read but not mutate; unauthenticated requests are rejected.
14. Replay bundles and golden cases contain **no secrets**: recorded model/tool payloads pass through `redact_secrets` before persistence; an integration test injecting an API key into a tool result asserts `«redacted»` in the stored bundle and the absence of the secret substring.
15. The whole golden gate runs in CI with `EVAL_LLM_JUDGE_ENABLED=false` and **zero live model/network calls** (retrieval against local pgvector, agent_task from cassettes, judge metrics skipped) and completes deterministically.
16. `GET /replay/{bundle_id}` returns the ordered `ReplayEvent` list (model/tool/retrieval steps) consumable by the step-level inspector, and a signed URL for the raw payload.
17. Every gate `metric` string in `gates.yaml` exactly matches a key in `EvalReport.aggregate` (and in the canonical flat-metric-key table); `compare_to_baseline` reads `aggregate[metric].mean`; a gate whose metric is absent from the report is reported `violated=True` (`reason="metric absent"`), never a silent pass. Verified by a test that loads `gates.yaml` and asserts every gated key is emitted by the tiny_suite run.
18. **Human-approval non-negotiable.** Every shipped `agent_task` golden case declares `expected_terminal_state` in {`pr_opened`, `awaiting_review`, `needs_human_input`} and **never** `merged`; the harness performs no merge and the `agent_target` never invokes a merge path; `forge eval lint-suite` exits `1` if any `agent_task` case sets `expected_terminal_state: merged`. (Asserted over the shipped suite + a synthetic bad case.)
19. **Spec-gating preserved.** Each `agent_task` cassette/case carries an approved `spec_ref`; the `agent_target` grades requirement satisfaction against that spec's acceptance criteria (`expected_satisfied_criteria`), and a case with no `spec_ref` is rejected by `lint-suite` — mirroring "no implementation run without an approved spec".
20. Triggering a run (`POST /runs`), promoting a baseline (`POST /baselines`), starting an A/B (`POST /ab`), and fetching a replay (`GET /replay/{bundle_id}`) each emit one immutable audit event via the F39 `AuditSink` with actor, `suite`/`arm`/`eval_run_id` (or `bundle_id`), and outcome; denied (`403`) attempts are audited as `denied`.

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Write tests first (backend-tdd, ≥80% coverage per the spec's own profile). Tests in `packages/evaluation/tests/` and `apps/api/tests/eval/`.

Key fixtures:
- `FakeLLMJudge` — deterministic faithfulness = fraction of answer tokens present in concatenated contexts; answer_relevancy = token-overlap ratio with the question. No network.
- `tiny_suite` — a 3-case in-memory suite (one per type) for harness wiring tests, independent of the shipped golden files.
- `seed_corpus` — reuse F05's seeded fixture repo (`reject_expired_token`, README, policy, plan.md) so retrieval cases are reproducible.
- `recorded_cassette` — a shipped `ReplayBundle` for one `agent_task` golden case (model + tool + retrieval events) enabling offline agent grading.
- `pg` + `minio` — testcontainers/CI service containers for integration tests (migrations applied, artifact store live).

Unit tests (pure, no DB/network):
- `test_recall_precision_mrr_hand_computed` and `test_ndcg_matches_graded_dcg` (AC3); `test_recall_empty_relevant_is_zero`.
- `test_ragas_context_metrics_deterministic` and `test_ragas_judge_metrics_none_without_judge` (AC4).
- `test_golden_loader_validates_and_rejects_mismatched_payload` (AC2); `test_golden_suite_has_at_least_30_cases` (AC2, reads shipped files).
- `test_compare_to_baseline_floor_and_regression` table-driven (AC7); `test_lower_is_better_inverts`; `test_no_baseline_applies_only_min_gates` (AC8); `test_absent_gated_metric_is_violation` (AC17).
- `test_every_gate_key_is_emitted_by_tiny_suite` — load `gates.yaml`, run `tiny_suite`, assert each gate `metric` ∈ `report.aggregate` keys (AC17).
- `test_ab_compare_deltas_and_winner` and `test_reranker_delta` (AC12).
- `test_summarize_mean_p50_p95_count` (aggregate); `test_rate_metrics_mean_equals_rate` (agent rate keys are 1.0/0.0 → mean is the rate).
- `test_replay_player_deterministic` and `test_replay_divergence_raises` (AC6).
- `test_recorded_payloads_are_redacted` (AC14, pure recorder path).
- `test_lint_suite_rejects_merged_terminal_state` (AC18) and `test_lint_suite_rejects_agent_case_without_spec_ref` (AC19).
- `test_shipped_agent_cases_never_terminal_merged` — reads shipped `agent_task` YAML, asserts `expected_terminal_state != "merged"` (AC18).

Integration tests (with `pg` + `minio` + fakes):
- `test_retrieval_target_end_to_end_against_seeded_retriever` (AC5).
- `test_spec_target_uses_deterministic_generator_and_traceability` (AC; spec.completeness + acceptance_criteria_coverage computed).
- `test_agent_target_replays_cassette_and_grades` (AC6) — asserts byte-identical metrics across two runs.
- `test_run_suite_persists_results_and_aggregate_and_report` (AC10).
- `test_gate_blocks_on_injected_regression` — inject a degraded arm, assert `regression_passed=false`.
- `test_set_baseline_atomic_swap` (AC11).
- `test_replay_bundle_redaction_persisted` (AC14, full path).

API tests (`apps/api/tests/eval/`, httpx AsyncClient, Celery eager):
- `test_start_run_requires_admin_and_enqueues` (AC13).
- `test_get_run_and_case_detail_scoped_to_workspace`.
- `test_set_baseline_requires_admin`; `test_agent_runner_read_only` (AC13).
- `test_get_replay_returns_ordered_events_and_signed_url` (AC16).
- `test_run_start_and_baseline_set_emit_audit_events` and `test_denied_run_start_is_audited` (AC20, against a fake/in-memory `AuditSink`).

CLI / CI tests:
- `test_cli_gate_exit_codes` — subprocess `forge eval gate` returns `0`/`1` for clean/regressed baselines (AC9).
- `test_cli_lint_suite_fails_on_bad_case` (AC2).
- `test_ci_gate_runs_without_model_keys` — run with `EVAL_LLM_JUDGE_ENABLED=false`, assert no outbound calls (network-blocking fixture) and a complete report (AC15).

DB test (`packages/db/tests/`):
- Extend `test_models.py::EXPECTED_MODELS` + `test_migration.py::EXPECTED_TABLES` with the four new models/tables and assert `test_table_set_is_exactly_the_spec_model` + `test_alembic_upgrade_creates_expected_tables` stay green (AC1).

Schema tests (`packages/db/tests/`): `test_create_all_includes_eval_tables_and_partial_index` (SQLite `create_all` produces the four tables + the partial unique active-baseline index) and `test_alembic_upgrade_head_then_downgrade_base_roundtrips` (baseline upgrade creates them, downgrade drops them, no double-create), plus the `EXPECTED_MODELS`/`EXPECTED_TABLES` extensions from §3.1 (AC1).

## 8. Security & policy considerations

- **Secret redaction (spec Security → "Secret redaction").** Every recorded replay payload (model prompts/completions, tool results, retrieval chunks) and any persisted `actual`/`expected` snapshot passes through F05's `redact_secrets` before storage. AC14 enforces. Golden YAML is reviewed in PR and must use synthetic fixtures only.
- **BYOK judge keys.** `EVAL_JUDGE_MODEL` credentials resolve from the encrypted per-workspace vault at call time; never logged, never written into reports, bundles, or metrics. CI runs with the judge disabled so no key is required.
- **Tenant isolation.** `eval_run`/`eval_case_result`/`replay_bundle` carry `workspace_id`; tenant-scoped reads filter by it. System-level CI runs use `workspace_id=NULL` and are admin-only. Replay payloads are fetched via signed URLs scoped to the requesting workspace.
- **RBAC & audit.** `admin` triggers runs / sets baselines / runs A/B; `member`/`agent-runner` are read-only (AC13). Run starts, baseline promotions, A/B starts, and replay fetches emit immutable audit events through the `cross-cutting/F39-audit-log` `AuditSink` (actor, suite, arm, `eval_run_id`/`bundle_id`, outcome) per the spec's immutable-audit requirement; denials are audited too (AC20).
- **Human-approval-before-merge non-negotiable.** The harness is read-only with respect to git/PRs: `agent_task` golden cases terminate at `pr_opened`/`awaiting_review`/`needs_human_input` and **never** `merged`; the `agent_target` exercises plan→execute→verify→PR but never the merge path, and `lint-suite` rejects a `merged` terminal state (AC18). Eval thus measures the agent up to the human gate without ever bypassing it.
- **MCP read-only default.** Retrieval cases that exercise the F09 query-through path use it strictly read-only (no MCP writes); eval never enables `allow_write`. This is inherent — the harness only reads/grades.
- **No anonymous access.** All eval routes (mounted under `Settings.api_prefix`, e.g. `/api/v1/eval/*`) require an authenticated `Principal`; unauthenticated requests get `401`.
- **Determinism = safety of the gate.** Because the gate blocks merges, gated metrics must be reproducible. LLM-judged RAGAS metrics are reported but **never gated** (only deterministic set-based and cassette-derived metrics gate), preventing a flaky judge from blocking or silently waving through merges.
- **Replay integrity.** `content_hash` over the canonicalized event list detects tampering/drift; divergence raises rather than masks. Cassettes are stored in MinIO with the same access controls as run artifacts.
- **DoS bounds.** `EVAL_CONCURRENCY` caps parallel case execution; per-case timeouts inherit F08's verification timeouts; report/bundle sizes are bounded and stored in object storage, not the DB.

## 9. Effort estimate & risk (S/M/L + key risks)

**Effort: L** — the metric library and gate are S–M, but the replay recorder/player (deterministic capture of model/tool/retrieval seams), the three eval targets wiring into F02/F05/F08, authoring ≥30 quality golden cases with cassettes, and the CI gate together make it L. Rough split: schema+migration S, metrics+gate+aggregate M, replay recorder/player M, targets M, golden authoring + cassettes M, API+CLI+CI S, UI S, tests M.

Key risks:
- **Replay divergence from legitimate code changes.** Refactors change the call sequence and break cassettes. Mitigation: stable content-addressed match keys, `ReplayDivergenceError` surfaced as an actionable failure, and a documented `forge eval record` re-capture flow; agent_task gates tolerate this by keying on `requirement_satisfaction_rate`/terminal state rather than exact transcripts. (Medium)
- **LLM-judge nondeterminism / cost.** Mitigation: judge metrics are reported-not-gated, disabled in CI by default, and computed via the deterministic `FakeLLMJudge` in tests; production judge calls are cached by `(question, answer, contexts)` hash. (Medium)
- **Golden-set staleness / metric gaming.** A small or stale suite gives false confidence. Mitigation: enforce ≥30 cases with type distribution in `lint-suite`, require golden additions for new feature slices in the contribution guide, and track per-case trend to spot overfitting. (Medium)
- **Baseline bootstrap.** No baseline on first run could either block everything or wave everything through. Mitigation: `EVAL_BASELINE_REQUIRED=false` default + absolute `min` gates so the first run is meaningful and promotable. (Low)
- **Fixture-repo realism.** Synthetic repos may not reflect real retrieval/agent difficulty. Mitigation: model fixtures on the F05 seed corpus and grow them from real (sanitized) cases over time. (Low)
- **CI runtime.** Full suite per PR could be slow. Mitigation: cassette replay is fast (no model calls), bounded concurrency, and a `--changed-only` fast path keyed on case tags. (Low)

## 10. Key files / paths (exact)

- `packages/db/forge_db/models/eval.py` — SQLAlchemy 2.x ORM for the four tables (incl. `__table_args__` indexes + partial unique active-baseline index); registered in `packages/db/forge_db/models/__init__.py` (`__all__` extended). No chained migration while `0001_baseline.py` is the sole metadata-driven migration (see §3.1); a guarded incremental `packages/db/migrations/versions/00NN_eval_harness.py` is added only once baseline is frozen post-release.
- `packages/db/tests/test_models.py`, `packages/db/tests/test_migration.py` — extend `EXPECTED_MODELS` / `EXPECTED_TABLES` with the four new models/tables (AC1).
- `packages/evaluation/forge_eval/models.py` — Pydantic models + enums + `SCHEMA_VERSION`.
- `packages/evaluation/forge_eval/protocols.py` — `GoldenLoader`, `LLMJudge`, `EvalTarget`, `Replayer`, `EvalRunRepository`.
- `packages/evaluation/forge_eval/golden/loader.py`, `golden/gates.yaml`, `golden/cases/{retrieval,spec,agent_task}/*.yaml` (≥30 cases + cassettes).
- `packages/evaluation/forge_eval/metrics/{retrieval.py,ragas.py,spec.py,workflow.py,cost.py,aggregate.py}`.
- `packages/evaluation/forge_eval/replay/{recorder.py,player.py,store.py}`.
- `packages/evaluation/forge_eval/targets/{retrieval_target.py,spec_target.py,agent_target.py}`.
- `packages/evaluation/forge_eval/harness/{runner.py,compare.py,ab.py}`, `report.py`.
- `packages/evaluation/tests/` — unit + integration tests and fixtures (`FakeLLMJudge`, `tiny_suite`, `recorded_cassette`).
- `apps/api/forge_api/routers/eval.py` — router (added to `FEATURE_ROUTERS` in `apps/api/forge_api/routers/__init__.py`).
- `apps/api/forge_api/services/eval_service.py` — DI wiring (session, MinIO, F37 vault + audit sink, F05/F02/F08, Celery).
- `apps/api/forge_api/schemas/eval.py` — request schemas.
- `apps/api/forge_api/cli/eval.py` — `forge eval run|gate|baseline|ab|lint-suite`.
- `apps/api/tests/eval/` — API + CLI tests.
- `apps/worker/forge_worker/tasks/eval.py` — `run_suite`, `run_case`, `run_ab`, `persist_replay_bundle`.
- `apps/web/app/(app)/eval/page.tsx`, `apps/web/app/(app)/eval/[runId]/page.tsx`, `apps/web/components/eval/CaseReplayDrawer.tsx`, `apps/web/lib/api/eval.ts`.
- `.github/workflows/eval-gate.yml` — CI regression gate.
- `deploy/.env.example`, `deploy/.env.production.example` — `EVAL_*` and `FORGE_RECORD_REPLAY` vars.

## 11. Research references (relevant links from the spec/research report)

- Spec: `docs/FORGE_SPEC.md` → "Observability and Evaluation" (golden set 30–100, regressions block merge, replayable runs, requirement-to-test traceability, A/B evaluation), "Key Metrics" (the workflow/agent/retrieval/cost metric families this slice computes), Core Design Principles #10 and Build Prompt non-negotiable #10 (golden set before framework complexity), Phase 1 roadmap item "Golden test set and evaluation harness (30+ tasks)".
- Research: `docs/forge-research-report.md` → "What Makes Forge Buildable" #3 "Eval-first development" (golden set of 30–100 representative pairs before orchestration complexity) and "Hybrid Retrieval" (reranking improves RAGAS metrics by 15–30% — the `reranker_delta`/A/B motivation).
- LangGraph production guide 2026 (ship smallest agent loop, build a golden eval set, use StateGraph + checkpointers): https://www.reactify-solutions.com/articles/langgraph-production-agents-2026
- RAGAS metric definitions (faithfulness, answer relevancy, context precision/recall) — referenced via the production RAG guide: https://lushbinary.com/blog/rag-retrieval-augmented-generation-production-guide/
- LangSmith (agent tracing; optional sink for replayable run traces): https://smith.langchain.com/
- Jina Reranker v2 (the rerank arm A/B-tested for RAGAS lift): https://jina.ai/reranker/

## 12. Out of scope / future

- **Public evaluation leaderboard / benchmark suite** — Phase 3 roadmap ("Benchmark suite and public evaluation leaderboard").
- **Multi-agent (supervised) evaluation** — Phase 3; V1 grades single-agent `agent_task` runs only.
- **Online / continuous production eval and canary scoring** — V1 is offline golden-suite + CI gate; live metric collection (retrieval latency p50/p95/p99, MCP freshness lag, PR acceptance rate, MTTM/MTTC, approval accept/reject) is `cross-cutting/F38-observability-cost-metrics`' job (F12 only consumes/aggregates golden-suite results).
- **Embedding-based semantic answer similarity and full RAGAS suite** (e.g. answer correctness, noise sensitivity) — V1 ships the four core RAGAS metrics; deeper RAGAS is future tuning once the golden set guides it.
- **Temporal-backed durable replay** — V1 replay is cassette-based; durable workflow replay arrives with the V2 Temporal engine.
- **Auto-generated golden cases from production runs** — future: promote sanitized real workflow runs into golden cases via the recorder.
- **A/B UI beyond the basic diff** — rich experiment tracking dashboards are future Observability work; V1 ships the `forge eval ab` diff and a metric table.
