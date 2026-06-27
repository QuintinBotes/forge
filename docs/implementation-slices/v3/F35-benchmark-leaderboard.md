# F35 — Benchmark Suite & Public Eval Leaderboard

> Phase: v3 · Spec module(s): Observability Layer (eval harness), `packages/evaluation` (`forge-eval`), OSS Strategy (community artifacts / extension points), Self-Hosting & Deployment (public surface) · Status target: **Done** = a benchmark is a **frozen, versioned, content-hashed** task set (distinct from F12's growable golden CI suite); running it under a configuration arm produces a **`BenchmarkSubmission`** with a deterministic, weighted **composite score** plus per-category/per-metric breakdown computed on top of an F12 `EvalReport`; submissions can be **verified** by deterministically replaying their shipped cassettes and reproducing the claimed composite within `epsilon` *and* matching every replay `content_hash` (gaming-resistant); a **public, unauthenticated, read-only, rate-limited leaderboard** (`/public/leaderboard/{slug}/{version}`) ranks moderated public+verified submissions by composite with verified badges and full provenance, exposing **no secrets and no raw payloads**; external contributors can submit a report+bundle for verification; admins moderate (publish/flag); the public leaderboard is **disabled by default** for self-hosted privacy; and a benchmark whose tasks change is **rejected unless its version is bumped** so historical scores stay comparable.

---

## 1. Intent — what & why

The Phase 3 roadmap lists, verbatim, *"Benchmark suite and public evaluation leaderboard"* (FORGE_SPEC.md §"Phased Roadmap → Phase 3 (V3)"). F12 (`docs/implementation-slices/v1/F12-eval-harness.md`) built the substrate — golden cases, metric evaluators, deterministic replay bundles with `content_hash`, the `EvalRunner`, the regression gate, and A/B comparison — and its §12 explicitly defers this feature: *"Public evaluation leaderboard / benchmark suite — Phase 3 roadmap."* F35 is that deferred slice.

The distinction between F12's golden suite and F35's benchmark is the whole point and must not be blurred:

| | F12 golden suite | F35 benchmark suite |
|---|---|---|
| Purpose | CI quality **gate** (block merge on regression vs a moving baseline) | **Compare configurations** and publish a **comparable, reproducible score** |
| Mutability | **Grows** over time (contributors add cases) | **Frozen + versioned** per `content_hash`; changing tasks requires a version bump |
| Audience | Maintainers / CI | The **public** OSS community (leaderboard) |
| Output | `RegressionResult` (pass/fail vs baseline) | `BenchmarkSubmission` with a `composite_score` ranked on a leaderboard |
| Trust model | Trusted internal CI | **Untrusted submitters** → requires replay-based **verification** + admin **moderation** |

F35 answers a question F12 cannot: *"Which model provider / agent mode / retrieval config produces the best Forge runs, and can anyone reproduce that number?"* This is the SWE-bench/HELM-style discipline — a frozen task set, a fixed scoring rubric, a public ranking, and bit-for-bit reproducibility — applied to Forge's own eval harness. It is governed by three non-negotiables drawn from the spec:

1. **Reproducible or it doesn't count (Observability & Evaluation: "Replayable workflow runs").** A leaderboard score is meaningless if it cannot be reproduced. Every leaderboard entry binds to (a) the benchmark `content_hash` (exact task set), (b) the submission's F12 replay bundles, and (c) the deterministic scoring function. Verification re-derives the score offline from the bundles; only verified submissions get the badge, and the public detail page ships a one-line reproduce command + signed bundle download.
2. **Untrusted input, fail-closed (Security: audit, redaction, RBAC).** Submissions arrive from outside the trust boundary. They are scored/verified deterministically, never auto-published; publishing requires an admin moderation action; the stored config and labels are secret-redacted (BYOK keys never persisted); the public surface exposes only moderated, published, payload-free data.
3. **Comparability is sacred (eval-first).** Once frozen, a benchmark version's tasks and scoring are immutable. Any change rejects unless `version` is bumped, so a `forge-swe-v1@1.0.0` score in January is the same yardstick in June.

Without F35, Forge has internal quality measurement (F12) but no public, reproducible, comparable way for the community to evaluate model/config choices — the explicit Phase 3 deliverable and a core OSS-adoption artifact.

---

## 2. User-facing behavior / journeys

- **Journey A — Maintainer freezes a benchmark.** A maintainer authors `packages/evaluation/forge_eval/benchmarks/forge-swe/1.0.0/manifest.yaml` + cases, then runs `forge bench freeze --suite forge-swe --version 1.0.0`. The CLI loads all cases, computes the canonical `content_hash` over (ordered cases + scoring), writes it into the manifest, marks the suite `frozen`, and registers a `benchmark_suite` row. Editing any case afterwards and re-running any benchmark command fails with `BenchmarkFrozenError: content_hash mismatch; bump version`.
- **Journey B — Internal operator runs a benchmark.** An admin runs `forge bench run --suite forge-swe --version 1.0.0 --arm config-opus-singleagent` (or `POST /api/v1/benchmarks/forge-swe/1.0.0/runs`). The worker executes the frozen task set through the F12 `EvalRunner` under the chosen `ArmConfig`, computes the composite via the benchmark's scoring rubric, and persists a `benchmark_submission` (`status=scored`, `visibility=private`). The operator reviews the score breakdown in the internal benchmark UI.
- **Journey C — External contributor submits a result.** A community member runs Forge against the public benchmark, then `forge bench submit --suite forge-swe --version 1.0.0 --report report.json --bundles ./bundles/`. This uploads the `EvalReport` + replay bundles and creates a `benchmark_submission` with `status=pending`. They get a submission id to track verification.
- **Journey D — Verification.** An admin (or a scheduled job) runs `forge bench verify <submission_id>` / `POST /api/v1/benchmarks/submissions/{id}/verify`. The verifier replays the submitted cassettes deterministically (F12 `ReplayPlayer`), recomputes the composite, and checks it matches the claimed composite within `BENCHMARK_VERIFY_EPSILON` **and** that every bundle `content_hash` matches. On success → `verified=true`; on mismatch → `status=rejected` with reasons.
- **Journey E — Moderation & publish.** An admin reviews a verified submission and clicks **Publish** (`POST /api/v1/benchmarks/submissions/{id}/publish`), flipping `visibility=public`. It now appears on the public leaderboard. A suspicious entry can be **flagged** (`/flag`), removing it.
- **Journey F — The public visits the leaderboard (no account).** Anyone opens `https://<host>/leaderboard/forge-swe/1.0.0` (served by the unauthenticated `/public/*` API). They see a ranked table: rank, model label, agent mode, composite, per-category bars, a green **Verified** badge, the Forge version under test, and submitter. They filter by agent mode / model family, sort by category, and click an entry to see its full provenance, score breakdown, and a **Reproduce** panel (the exact `forge bench verify` command + signed download links for the replay bundles). No secrets, no raw prompts/diffs.
- **Journey G — Self-hoster keeps it private.** A self-hosted operator leaves `PUBLIC_LEADERBOARD_ENABLED=false` (the default). The `/public/*` routes return `404`, so internal benchmark scores never leak; they still use the authenticated internal benchmark UI.

---

## 3. Vertical slice

> Layout note: this slice uses the **actual** in-tree repo layout established by F12 — `apps/api/forge_api` (routers in `forge_api/routers/`, registered in `routers/__init__.py::FEATURE_ROUTERS`, mounted under `Settings.api_prefix`; CLI sub-command groups in `forge_api/cli/` on the `forge-cli` console script), `apps/worker/forge_worker`, `packages/db/forge_db` (ORM in `forge_db/models/`), the Alembic config at `packages/db/alembic.ini` with migrations in `packages/db/migrations/versions/`, and the eval core in `packages/evaluation/forge_eval`. The repo is **metadata-driven**: the sole migration `0001_baseline.py` runs `Base.metadata.create_all`, so new tables enter the schema by registering their ORM models on `Base.metadata` (see §3.1) — F12 added **no** chained migration and neither does F35. F35 is strictly additive on top of F12 — it reuses `eval_run`, `eval_case_result`, `replay_bundle`, the `EvalRunner`, the metric library, and the replay player/store without modifying them.

### 3.1 Data model (tables/columns/migrations touched)

F35 follows the repo's **metadata-driven** schema convention (identical to F12): the two new ORM models live in `packages/db/forge_db/models/benchmark.py` (`BenchmarkSuite`, `BenchmarkSubmission`) and are registered on `Base.metadata` via `forge_db/models/__init__.py` (extend `__all__`). Because the baseline migration `packages/db/migrations/versions/0001_baseline.py` runs `Base.metadata.create_all`, both fresh installs and the SQLite unit suite create the tables automatically — **no chained `op.create_table` migration is added** while `0001_baseline.py` is the sole migration (it would double-create what `create_all` already makes and break `alembic upgrade head`). All indexes (`UNIQUE (slug, version)`, the leaderboard covering index) are declared in each model's `__table_args__` so `create_all` emits them. **Required test update (load-bearing, mirrors F12):** extend `packages/db/tests/test_models.py::EXPECTED_MODELS` with `BenchmarkSuite`/`BenchmarkSubmission` and `packages/db/tests/test_migration.py::EXPECTED_TABLES` with `benchmark_suite`/`benchmark_submission` (both assert the model/table set is **exactly** the spec set). (The identical DDL ships as an incremental `packages/db/migrations/versions/00NN_benchmark_leaderboard.py` only once the baseline is later frozen post-release, guarded with an inspector `has_table` no-op check and `down_revision` set to the then-current head; `downgrade` drops both tables.)

Benchmark **tasks are file-based** (canonical source = YAML under `packages/evaluation/forge_eval/benchmarks/<slug>/<version>/`, same model as F12 golden cases); the DB stores the frozen suite registration, submissions, and their scores. F35 **reuses** `eval_run` (one per submission run) and `eval_case_result` / `replay_bundle` (per task) — no new per-task table.

**Table `benchmark_suite`** — registration + freeze record for one immutable benchmark version.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | `gen_random_uuid()` |
| `slug` | `text` NOT NULL | e.g. `forge-swe` |
| `version` | `text` NOT NULL | e.g. `1.0.0` (semver) |
| `title` | `text` NOT NULL | |
| `description` | `text` NOT NULL DEFAULT `''` | |
| `task_count` | `integer` NOT NULL | number of frozen cases |
| `primary_metric` | `text` NOT NULL DEFAULT `'benchmark.composite'` | headline ranking key |
| `scoring` | `jsonb` NOT NULL | serialized `BenchmarkScoring` (weights, direction) |
| `content_hash` | `text` NOT NULL | sha256 over canonicalized ordered cases + scoring — the reproducibility anchor |
| `frozen` | `boolean` NOT NULL DEFAULT `false` | immutable once `true` |
| `published` | `boolean` NOT NULL DEFAULT `false` | suite is selectable on the public leaderboard |
| `created_by` | `uuid` NULL FK → `app_user.id` | NULL for system/CI |
| `created_at` | `timestamptz` NOT NULL DEFAULT now() | |

Indexes (declared in `__table_args__`): `UNIQUE (slug, version)`; btree `(published, slug)`.

**Table `benchmark_submission`** — one scored attempt of a frozen suite under a config.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `benchmark_suite_id` | `uuid` NOT NULL FK → `benchmark_suite.id` ON DELETE CASCADE | bound to the exact frozen version |
| `suite_content_hash` | `text` NOT NULL | copied at submit time; must equal the suite's hash (drift guard) |
| `workspace_id` | `uuid` NULL FK → `workspace.id` ON DELETE CASCADE | NULL = official/system submission |
| `eval_run_id` | `uuid` NULL FK → `eval_run.id` ON DELETE SET NULL | underlying F12 run (null for external report-only ingests until scored) |
| `submitter_name` | `text` NOT NULL | public display name |
| `submitter_org` | `text` NULL | public, optional |
| `submitter_contact` | `text` NULL | **private** (email/url); never returned by `/public/*` |
| `model_label` | `text` NOT NULL | public family label, e.g. `claude-opus / anthropic` (no keys) |
| `agent_mode` | `text` NOT NULL | `single_agent \| supervised_multi_agent` |
| `config` | `jsonb` NOT NULL DEFAULT `'{}'` | **redacted** `ArmConfig` + run knobs; no secrets |
| `forge_version` | `text` NULL | git sha / release tag of Forge under test |
| `composite_score` | `numeric(6,4)` NULL | the leaderboard number (0..1); NULL until scored |
| `scores` | `jsonb` NOT NULL DEFAULT `'{}'` | serialized `BenchmarkScore` (per-metric + per-category) |
| `replay_content_hashes` | `jsonb` NOT NULL DEFAULT `'[]'` | ordered bundle hashes for verification |
| `status` | `text` NOT NULL DEFAULT `'pending'` | `pending \| scoring \| scored \| verified \| rejected \| flagged` |
| `visibility` | `text` NOT NULL DEFAULT `'private'` | `private \| public` |
| `verified` | `boolean` NOT NULL DEFAULT `false` | |
| `verified_at` | `timestamptz` NULL | |
| `verification` | `jsonb` NULL | serialized `VerificationResult` (claimed vs reproduced, deltas, reasons) |
| `moderated_by` | `uuid` NULL FK → `app_user.id` | admin who published/flagged |
| `submitted_by` | `uuid` NULL FK → `app_user.id` | NULL for external/anonymous-source ingests |
| `submitted_at` | `timestamptz` NOT NULL DEFAULT now() | |

Indexes (declared in `__table_args__`): btree `(benchmark_suite_id, visibility, verified, composite_score DESC)` — the leaderboard covering index; btree `(workspace_id, submitted_at DESC)`; btree `(status)`.

Both tables are created by `Base.metadata.create_all` (§3.1); the FKs to F12 tables (`eval_run`) are `ON DELETE SET NULL`-safe and no F12 tables are altered. The once-frozen incremental migration's `downgrade` drops both tables cleanly.

**Leaderboard is computed, not stored.** The ranking is a query over `benchmark_submission` (`visibility='public' AND status IN ('verified','scored')`, ordered by the covering index) wrapped in a service that assigns ranks + tie-breaks. The public read path is cache-friendly (HTTP `Cache-Control` + a short in-process TTL); a materialized snapshot table is deliberately deferred (§12).

### 3.2 Backend (FastAPI routes + services/packages)

**`packages/evaluation/forge_eval/benchmark/` (NEW — framework-agnostic, pure; no FastAPI/SQLAlchemy):**

```
packages/evaluation/forge_eval/benchmark/
├── __init__.py
├── models.py        # BenchmarkScoring, BenchmarkManifest, BenchmarkScore, CategoryScore,
│                    #   VerificationResult, LeaderboardRow, SubmissionStatus, Visibility (§4)
├── manifest.py      # load_manifest(dir) -> BenchmarkManifest; compute_content_hash(cases, scoring)
│                    #   -> str; freeze(manifest, cases) (validates immutability)
├── scoring.py       # compute_benchmark_score(report, scoring, cases) -> BenchmarkScore (pure)
├── verify.py        # verify_submission(...) -> VerificationResult (deterministic replay re-score)
├── leaderboard.py   # rank_submissions(rows) -> list[LeaderboardRow] (pure ranking + tie-break)
└── errors.py        # BenchmarkFrozenError, BenchmarkContentHashMismatch, BenchmarkVerificationError
```

These reuse F12 directly: `manifest.py` loads cases via F12's `YAMLGoldenLoader`/`GoldenCase`; `scoring.py` consumes an F12 `EvalReport.aggregate`; `verify.py` uses F12's `ReplayPlayer`, `ReplayBundle.content_hash`, and re-invokes the F12 metric functions + `compute_benchmark_score`.

**Service `apps/api/forge_api/services/benchmark_service.py` (NEW):** wires `forge_eval.benchmark` to the async SQLAlchemy session, MinIO (bundle storage + signed URLs), the BYOK vault (judge/model keys for live runs), the F12 `eval_service`/`EvalRunner`, and Celery.
- `register_suite(manifest, cases, *, frozen) -> BenchmarkSuiteOut`
- `start_run(slug, version, arm, *, workspace_id, user_id) -> submission_id` (enqueues Celery)
- `ingest_submission(slug, version, report, bundle_keys, meta, *, workspace_id, user_id) -> submission_id` (external path; `status=pending`)
- `verify(submission_id) -> VerificationResult` (enqueues Celery `benchmark.verify_submission`)
- `publish(submission_id, *, moderator_id)` / `flag(submission_id, *, moderator_id, reason)` (admin moderation; sets `visibility`/`status`)
- `leaderboard(slug, version, *, public_only: bool) -> list[LeaderboardRow]` (computed ranking; `public_only=True` for the public router)

**Authenticated management router `apps/api/forge_api/routers/benchmarks.py`** (`APIRouter(prefix="/benchmarks", tags=["benchmarks"])`, added to `FEATURE_ROUTERS` in `routers/__init__.py` so it mounts under `Settings.api_prefix` → `/api/v1/benchmarks`; all routes authenticated via the foundation auth dependency + workspace-scoped; RBAC enforced with F37's `require_role(...)`):**

| Method | Path | RBAC | Purpose |
|---|---|---|---|
| `GET` | `/benchmarks` | member | List registered (frozen) suites. |
| `GET` | `/benchmarks/{slug}/{version}` | member | Suite manifest + `content_hash` + scoring. |
| `POST` | `/benchmarks/{slug}/{version}/runs` | admin | Enqueue an internal benchmark run → submission (`StartBenchmarkRequest`). |
| `POST` | `/benchmarks/{slug}/{version}/submissions` | member | External submit: report + bundle refs (`SubmitBenchmarkRequest`) → `pending`. |
| `GET` | `/benchmarks/{slug}/{version}/submissions` | member | List submissions (own workspace + own). |
| `GET` | `/benchmarks/submissions/{id}` | member | Submission detail incl. `verification` + per-task `eval_case_result` links. |
| `POST` | `/benchmarks/submissions/{id}/verify` | admin | Enqueue verification. |
| `POST` | `/benchmarks/submissions/{id}/publish` | admin | Moderate → `visibility=public` (requires `verified=true` unless `--force`, audited). |
| `POST` | `/benchmarks/submissions/{id}/flag` | admin | Flag/reject (`{reason}` → `status=flagged`). |

**Public unauthenticated router `apps/api/forge_api/routers/public_leaderboard.py`** (`APIRouter(prefix="/public", tags=["public-leaderboard"])`). It is deliberately **NOT** in `FEATURE_ROUTERS` (those carry the auth dependency and mount under `api_prefix`); instead the app factory (`forge_api/main.py`) calls `app.include_router(public_leaderboard_router)` directly at app root **only when `PUBLIC_LEADERBOARD_ENABLED=true`**, so it has no auth dependency, is read-only, and is rate-limited. When the flag is `false` (default) the router is never included → every `/public/*` path returns `404`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/public/benchmarks` | List **published** suites (slug, version, title, task_count, primary_metric). |
| `GET` | `/public/leaderboard/{slug}/{version}` | Ranked public+(verified/scored) submissions → `PublicLeaderboard`. Verified-first within score ties; payload-free. |
| `GET` | `/public/leaderboard/{slug}/{version}/submissions/{id}` | `PublicSubmissionDetail`: provenance, score breakdown, signed bundle download URLs, reproduce command. 404 if not public. |

This router uses a dedicated rate-limit dependency (`LEADERBOARD_PUBLIC_RATE_LIMIT`, per-IP), sets `Cache-Control: public, max-age=60`, permissive read-only CORS, and serializes via the `Public*` models in §4 which **cannot** carry `submitter_contact`, raw config secrets, or raw payloads.

**CLI `apps/api/forge_api/cli/bench.py` (NEW — registered as the `bench` sub-command group on the `forge-cli` console script, mirroring F12's `apps/api/forge_api/cli/eval.py`; `forge bench …` below is shorthand for `forge-cli bench …`, e.g. `docker compose exec api forge-cli bench verify <id>`):**
- `forge bench list`
- `forge bench freeze --suite <slug> --version <v>` — compute + write `content_hash`, register `frozen` suite (exit `1` on content drift after freeze).
- `forge bench run --suite <slug> --version <v> --arm <config> [--publish]`
- `forge bench submit --suite <slug> --version <v> --report report.json --bundles <dir>` — external contributor path.
- `forge bench verify <submission_id>` — exit `0` verified / `1` rejected, printing claimed vs reproduced.
- `forge bench leaderboard --suite <slug> --version <v> [--public]` — print the ranking.

### 3.3 Worker / agent runtime (Celery tasks, LangGraph, if any)

Celery tasks `apps/worker/forge_worker/tasks/benchmark.py` (new queue `benchmark`):

- `benchmark.run_submission(submission_id: str) -> dict` — load the frozen suite cases (assert their recomputed `content_hash == benchmark_suite.content_hash`, else fail `scored=false`), create an `eval_run`, drive the F12 `EvalRunner.run(...)` under the submission's `ArmConfig`, compute `BenchmarkScore` via `compute_benchmark_score(report, scoring, cases)`, persist `composite_score`/`scores`/`replay_content_hashes`, set `status=scored`. Reuses F12's `eval.run_suite` internals (this *is* an eval run over a frozen suite).
- `benchmark.verify_submission(submission_id: str) -> dict` — fetch the submitted replay bundles from MinIO, replay each deterministically via F12 `ReplayPlayer` (no model/network), recompute the `EvalReport` + `BenchmarkScore`, and call `verify_submission(...)`: pass iff `abs(reproduced.composite - claimed.composite) <= epsilon` **and** every recomputed bundle `content_hash` matches `replay_content_hashes`. Set `verified`/`verified_at`/`verification`; on failure set `status=rejected`. **No new LangGraph node** — verification is pure F12 replay.

**No agent-runtime modification.** F35 consumes the recorder/player F12 already ships; the benchmark run simply requests `FORGE_RECORD_REPLAY=true` so the submission's bundles are captured for later verification/publication. Heavy benchmark runs are manual or scheduled (not per-PR), with `EVAL_CONCURRENCY` bounding parallel cases.

### 3.4 Frontend / UI (Next.js routes/components, if any)

**Public (unauthenticated) leaderboard** — under a public route group with no auth guard, gated client-side on the `/public/benchmarks` 404 (feature disabled):
- `apps/web/app/(public)/leaderboard/page.tsx` — published-suite picker.
- `apps/web/app/(public)/leaderboard/[slug]/[version]/page.tsx` — ranked `LeaderboardTable` (TanStack Table): rank, model label, agent mode, composite, per-category bars, **Verified** badge, Forge version, submitter; filter by agent mode / model family; sort by any category. Hits `/public/leaderboard/...`.
- `apps/web/app/(public)/leaderboard/[slug]/[version]/submissions/[id]/page.tsx` — `SubmissionProvenance` (config labels, Forge version, dates), `ScoreBreakdown` (composite + per-category/per-metric), and `ReproducePanel` (copyable `forge bench verify` command + signed bundle download links).

**Internal (authenticated) benchmark admin** — under the app group:
- `apps/web/app/(app)/benchmarks/page.tsx` — suites + a "Run benchmark" action (admin) and the moderation queue (pending/verified submissions with Verify / Publish / Flag).
- `apps/web/app/(app)/benchmarks/submissions/[id]/page.tsx` — internal submission detail with `verification` result and per-task drill-down **reusing F10 `RunTraceTimeline` / F12 `CaseReplayDrawer`**.
- `apps/web/lib/api/benchmark.ts` — typed clients for `/api/v1/benchmarks/*`; `apps/web/lib/api/public-leaderboard.ts` — typed client for `/public/*`.

### 3.5 Infra / deploy (compose, helm, caddy, if any)

- **No new container.** Benchmarking runs on the existing `worker` (new `benchmark` queue), stores bundles/reports in the existing `minio`, reads/writes the existing `db`, and reuses the F05 `reranker` for retrieval cases.
- **Caddy (`deploy/caddy/Caddyfile`) + Nginx (`deploy/nginx/forge.conf`):** route `/public/*` to the API without auth, with a rate-limit directive and `Cache-Control` passthrough. The web `/leaderboard/*` route is statically reachable. (No-op when `PUBLIC_LEADERBOARD_ENABLED=false`, since the API returns `404`.)
- **CI workflow `.github/workflows/benchmark-smoke.yml`** — on `pull_request` touching `packages/evaluation/**`: run a tiny 3-task fixture benchmark from shipped cassettes through `forge bench run` + `forge bench verify` to guard that freeze/score/verify/leaderboard don't break. This is a **smoke test, not a gate** (the merge gate remains F12's `eval-gate`); heavy real benchmarks are run manually/scheduled.
- **Env vars** (add to `deploy/.env.example`, `deploy/.env.production.example`): `PUBLIC_LEADERBOARD_ENABLED` (default `false`), `BENCHMARK_DIR` (default packaged path), `BENCHMARK_VERIFY_EPSILON` (default `0.005`), `LEADERBOARD_PUBLIC_RATE_LIMIT` (default `60/minute`), `LEADERBOARD_CACHE_TTL_SECONDS` (default `60`), `BENCHMARK_SUBMISSION_MAX_BYTES` (default `52428800`).

---

## 4. Public interfaces / contracts (exact signatures, Pydantic models, Protocols, YAML schemas)

`packages/evaluation/forge_eval/benchmark/models.py` (reuses F12 `forge_eval.models`: `EvalReport`, `GoldenCase`, `ReplayBundle`, `MetricSummary`):

```python
from __future__ import annotations
from datetime import datetime
from enum import StrEnum
from uuid import UUID
from pydantic import BaseModel, Field, model_validator
from forge_eval.models import EvalReport, GoldenCase, ReplayBundle  # F12

BENCHMARK_SCHEMA_VERSION = 1

class SubmissionStatus(StrEnum):
    pending = "pending"; scoring = "scoring"; scored = "scored"
    verified = "verified"; rejected = "rejected"; flagged = "flagged"

class Visibility(StrEnum):
    private = "private"; public = "public"

class BenchmarkScoring(BaseModel):
    """Frozen scoring rubric. metric_weights are normalized to sum 1 at scoring time."""
    primary_metric: str = "benchmark.composite"
    metric_weights: dict[str, float]                 # dotted F12 metric -> weight, e.g.
                                                     #   {"agent.requirement_satisfaction_rate":0.5,
                                                     #    "retrieval.ndcg_at_k":0.3,"spec.completeness":0.2}
    direction: dict[str, str] = Field(default_factory=dict)  # metric -> higher_is_better|lower_is_better
    category_field: str = "tags"                     # which GoldenCase field groups categories
    @model_validator(mode="after")
    def _weights_positive(self) -> "BenchmarkScoring":
        if not self.metric_weights or any(w <= 0 for w in self.metric_weights.values()):
            raise ValueError("metric_weights must be non-empty and strictly positive")
        return self

class BenchmarkManifest(BaseModel):
    slug: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    title: str
    description: str = ""
    scoring: BenchmarkScoring
    case_files: list[str]                            # relative paths under the version dir
    schema_version: int = BENCHMARK_SCHEMA_VERSION
    content_hash: str | None = None                  # required+verified once frozen
    frozen: bool = False
    @model_validator(mode="after")
    def _frozen_requires_hash(self) -> "BenchmarkManifest":
        if self.frozen and not self.content_hash:
            raise ValueError("frozen manifest requires content_hash")
        return self

class CategoryScore(BaseModel):
    category: str; score: float; weight: float; case_count: int

class BenchmarkScore(BaseModel):
    composite: float                                 # 0..1 normalized leaderboard number
    per_metric: dict[str, float]                     # contributing metric means
    per_category: list[CategoryScore]
    total_cases: int; passed: int; errored: int

class VerificationResult(BaseModel):
    verified: bool
    claimed_composite: float
    reproduced_composite: float
    score_delta: float
    epsilon: float
    bundle_hash_matches: bool
    reasons: list[str] = Field(default_factory=list) # populated when verified=False

class LeaderboardRow(BaseModel):
    rank: int
    submission_id: UUID
    model_label: str
    agent_mode: str
    composite_score: float
    verified: bool
    forge_version: str | None
    submitter_name: str
    submitter_org: str | None = None
    per_category: list[CategoryScore]
    submitted_at: datetime
```

`packages/evaluation/forge_eval/benchmark/manifest.py` & `scoring.py` & `verify.py` & `leaderboard.py` (load-bearing pure functions):

```python
def compute_content_hash(cases: Sequence[GoldenCase], scoring: BenchmarkScoring) -> str:
    """sha256 over canonical JSON of [case.model_dump(sorted) for case in sorted(cases by id)]
    plus scoring.model_dump(sorted). Order-independent of file listing; stable across runs."""

def load_manifest(version_dir: Path) -> tuple[BenchmarkManifest, list[GoldenCase]]:
    """Load manifest.yaml + referenced cases via F12 YAMLGoldenLoader. If manifest.frozen,
    assert compute_content_hash(cases, scoring) == manifest.content_hash else raise
    BenchmarkContentHashMismatch."""

def freeze(manifest: BenchmarkManifest, cases: Sequence[GoldenCase]) -> BenchmarkManifest:
    """Return a copy with content_hash set and frozen=True. Raises BenchmarkFrozenError if
    already frozen with a different recomputed hash."""

def compute_benchmark_score(report: EvalReport, scoring: BenchmarkScoring,
                            cases: Sequence[GoldenCase]) -> BenchmarkScore:
    """Pure. For each gated metric in scoring.metric_weights, read report.aggregate[metric].mean
    (missing -> 0.0), apply direction (lower_is_better -> 1 - x clamped to [0,1]), then
    composite = Σ(w_i * x_i) / Σ(w_i). per_category groups report.results by the GoldenCase
    category_field and averages each case's normalized primary score. Deterministic; no I/O."""

def verify_submission(*, claimed: BenchmarkScore, reproduced_report: EvalReport,
                      reproduced_bundles: Sequence[ReplayBundle],
                      claimed_bundle_hashes: Sequence[str],
                      scoring: BenchmarkScoring, cases: Sequence[GoldenCase],
                      epsilon: float) -> VerificationResult:
    """reproduced = compute_benchmark_score(reproduced_report, scoring, cases).
    verified = abs(reproduced.composite - claimed.composite) <= epsilon
              AND [b.content_hash for b in reproduced_bundles] == list(claimed_bundle_hashes).
    Populates reasons on any failure. Pure (replay already executed by the caller)."""

def rank_submissions(rows: Sequence[LeaderboardRow]) -> list[LeaderboardRow]:
    """Stable ranking: sort by composite_score DESC, then verified DESC (verified outranks
    unverified on a tie), then submitted_at ASC (earliest first). Assigns 1-based rank;
    equal (composite, verified) share a rank (competition ranking)."""
```

`forge_eval.benchmark` consumes these F12 contracts (unchanged): `GoldenLoader`, `EvalRunner.run`, `ReplayPlayer.play`, `EvalReport`, `MetricSummary`, `ReplayBundle.content_hash`.

**FastAPI request/response schemas (`apps/api/forge_api/schemas/benchmark.py`):**

```python
from forge_eval.benchmark.models import (BenchmarkScore, CategoryScore,
                                          VerificationResult, SubmissionStatus, Visibility)
from forge_eval.models import ArmConfig

# ---- authenticated management ----
class StartBenchmarkRequest(BaseModel):
    arm: ArmConfig = ArmConfig()
    model_label: str
    agent_mode: str = "single_agent"
    forge_version: str | None = None
    publish_on_verify: bool = False

class SubmitBenchmarkRequest(BaseModel):
    submitter_name: str
    submitter_org: str | None = None
    submitter_contact: str | None = None         # private
    model_label: str
    agent_mode: str = "single_agent"
    forge_version: str | None = None
    config: dict = Field(default_factory=dict)   # redacted by the service before persist
    report: EvalReport
    bundle_object_keys: list[str]                # uploaded replay bundle keys (MinIO)
    claimed: BenchmarkScore

class BenchmarkSuiteOut(BaseModel):
    slug: str; version: str; title: str; description: str
    task_count: int; primary_metric: str; content_hash: str
    frozen: bool; published: bool

class SubmissionOut(BaseModel):                   # authenticated view (workspace-scoped)
    id: UUID; benchmark_slug: str; benchmark_version: str
    model_label: str; agent_mode: str; forge_version: str | None
    composite_score: float | None; scores: BenchmarkScore | None
    status: SubmissionStatus; visibility: Visibility
    verified: bool; verification: VerificationResult | None
    submitter_name: str; submitter_org: str | None
    submitted_at: datetime
    # NOTE: submitter_contact is intentionally excluded from all response models.

class PublishRequest(BaseModel): force: bool = False
class FlagRequest(BaseModel): reason: str = Field(min_length=1)

# ---- public (unauthenticated) — payload-free, secret-free ----
class PublicLeaderboardEntry(BaseModel):
    rank: int; model_label: str; agent_mode: str
    composite_score: float; verified: bool; forge_version: str | None
    submitter_name: str; submitter_org: str | None
    per_category: list[CategoryScore]; submitted_at: datetime
    submission_id: UUID

class PublicLeaderboard(BaseModel):
    slug: str; version: str; title: str; primary_metric: str
    content_hash: str; generated_at: datetime
    entries: list[PublicLeaderboardEntry]

class PublicSubmissionDetail(BaseModel):
    submission_id: UUID; slug: str; version: str
    model_label: str; agent_mode: str; forge_version: str | None
    composite_score: float; verified: bool
    scores: BenchmarkScore                        # breakdown only, no raw payloads
    submitter_name: str; submitter_org: str | None; submitted_at: datetime
    reproduce_command: str                        # e.g. "forge bench verify <id>"
    replay_bundle_urls: list[str]                 # short-lived signed download URLs
```

**Benchmark manifest YAML (`benchmarks/forge-swe/1.0.0/manifest.yaml`):**

```yaml
slug: forge-swe
version: "1.0.0"
title: "Forge SWE Benchmark v1"
description: "Frozen retrieval + spec + single-agent task set for cross-config comparison."
schema_version: 1
frozen: true
content_hash: "sha256:…"          # written by `forge bench freeze`
scoring:
  primary_metric: benchmark.composite
  metric_weights:
    agent.requirement_satisfaction_rate: 0.5
    retrieval.ndcg_at_k: 0.3
    spec.completeness: 0.2
  direction:
    agent.requirement_satisfaction_rate: higher_is_better
    retrieval.ndcg_at_k: higher_is_better
    spec.completeness: higher_is_better
  category_field: tags
case_files:
  - cases/retrieval/auth-expired-token.yaml
  - cases/spec/customer-endpoint.yaml
  - cases/agent_task/add-pagination.yaml
  # … frozen set (≥ task_count cases); each is a standard F12 GoldenCase YAML
```

---

## 5. Dependencies — features/slices that must exist first

- **`v1/F12-eval-harness`** — **REQUIRED, hard.** F35 *is* the Phase-3 extension F12 §12 defers. It imports `forge_eval.models` (`GoldenCase`/`EvalReport`/`ReplayBundle`/`ArmConfig`/`MetricSummary`), `YAMLGoldenLoader`, `EvalRunner`, the metric library, `ReplayPlayer`/`content_hash`, `eval_service`, and the `eval_run`/`eval_case_result`/`replay_bundle` tables — all reused unmodified. Every benchmark run is an F12 eval run over a frozen suite.
- **`v1/F00-foundation-substrate`** — **REQUIRED, hard** (placeholder slug matching F12's reconciliation note: the platform scaffold has no single dedicated file yet — reconcile to the final foundation slug when it lands). Provides the uv workspace; `apps/api` FastAPI skeleton (`forge_api`, `routers/__init__.py::FEATURE_ROUTERS`, `Settings.api_prefix`, the auth dependency, the app factory in `forge_api/main.py` where the public router is conditionally mounted); async SQLAlchemy 2.x session + shared `Base` in `packages/db` (`forge_db`); the `0001_baseline.py` Alembic baseline (`create_all`) + `packages/db/alembic.ini`; the `apps/worker` Celery app + queues; the `forge-cli` console script; the MinIO `ArtifactStore` (+ signed URLs); and the per-IP rate-limit utility the public router depends on.
- **`cross-cutting/F37-auth-secrets-byok`** — **REQUIRED, hard.** The auth `Principal` + `require_role(...)` RBAC dependency (`admin`/`member`/`viewer`/`agent-runner`), the encrypted per-workspace **BYOK** vault that resolves model/judge credentials at call time (never logged/persisted), and the canonical `SecretRedactor` reused alongside F05's `redact_secrets` for config/label redaction on ingest (§8, AC15).
- **`cross-cutting/F39-audit-log`** — **REQUIRED, hard.** The central immutable `AuditEvent`/`AuditSink` + `SqlAuditWriter`; F35 emits `benchmark.run_started`, `benchmark.submitted`, `benchmark.verified`, `benchmark.published`, and `benchmark.flagged` audit events (actor, submission/suite id, action, reason) satisfying the spec's immutable-audit requirement (§8, AC23). Degrades to local structured logging if the audit slice lags.
- **`v1/F05-hybrid-knowledge-retrieval`** — **REQUIRED, hard (transitively via F12)** — retrieval benchmark cases score `HybridRetrievalService`; reuses `RankedChunk.source_uri` and `redact_secrets`.
- **`v1/F02-spec-engine`** — **REQUIRED, hard (transitively via F12)** — spec benchmark cases use the deterministic spec generator + validation traceability.
- **`v1/F08-plan-execute-verify-pr-approval`** — **REQUIRED, hard (transitively via F12)** — `agent_task` cases replay F08's orchestrator flow and grade against acceptance criteria.
- **`v1/F10-run-trace-viewer`** — **SOFT** — the internal submission detail reuses `RunTraceTimeline`/F12 `CaseReplayDrawer` for per-task drill-down; the public leaderboard does not need it.
- **`v1/F14-docker-compose-selfhost` / `v1/F15-selfhosting-docs`** — **SOFT** — the `/public/*` Caddy/Nginx route and the `PUBLIC_LEADERBOARD_ENABLED` privacy note land in their compose/docs; F35 ships the route + env vars regardless.
- **`v3/F27-supervised-multi-agent`** — **SOFT** — enables `agent_mode=supervised_multi_agent` submissions as a comparison dimension; the benchmark, scoring, verification, and leaderboard are fully functional for `single_agent` submissions without it.
- **Downstream (NOT prerequisites; benefit from F35):** `v3/F32-integration-marketplace` (community can rank skill-profile/MCP-connector configs via benchmark submissions) and any "best config" recommendation surface.

---

## 6. Acceptance criteria (numbered, testable)

1. **Freeze determinism & content hash.** `compute_content_hash(cases, scoring)` is order-independent (shuffling the case list yields the same hash) and stable across processes; `forge bench freeze` writes it into the manifest and registers a `benchmark_suite` row with `frozen=true`.
2. **Frozen immutability.** After freeze, mutating any case body and calling `load_manifest`/any benchmark command raises `BenchmarkContentHashMismatch`; the only resolution is a `version` bump (a new `(slug, version)` row), so prior scores remain comparable.
3. **Composite scoring is exact and deterministic.** For an `EvalReport` whose `aggregate` has `agent.requirement_satisfaction_rate.mean=0.8`, `retrieval.ndcg_at_k.mean=0.6`, `spec.completeness.mean=1.0` and weights `0.5/0.3/0.2`, `compute_benchmark_score(...).composite == 0.78` (hand-computed); recomputing yields byte-identical `BenchmarkScore`.
4. **Direction handling.** A `lower_is_better` metric contributes `1 - mean` (clamped to `[0,1]`); a metric absent from `report.aggregate` contributes `0.0` without raising.
5. **Per-category breakdown (each case in exactly one category).** A case's category is derived as `case[category_field][0]` when that field is a non-empty list (e.g. the first `tag`), else `str(case[category_field])`, falling back to `case.case_type` when the field is absent/empty. `per_category` groups `report.results` (joined to `cases` by `case_id`) into these categories, reporting `case_count`, the category mean of `CaseResult.score`, and `weight = case_count / total_cases`; Σ`case_count` across categories equals `total_cases` (every case counted exactly once) and Σ`weight == 1.0`.
6. **Internal run produces a scored submission.** `benchmark.run_submission` over the fixture suite creates an `eval_run`, persists `eval_case_result` rows (reusing F12), and a `benchmark_submission` with `status=scored`, populated `composite_score`/`scores`/`replay_content_hashes`, `visibility=private`.
7. **Run guards the suite hash.** If the on-disk cases no longer hash to `benchmark_suite.content_hash`, `run_submission` fails the submission (`status` not advanced to `scored`) rather than scoring against a drifted set.
8. **Verification accepts a faithful submission.** Given a submission whose shipped bundles reproduce its claimed composite within `epsilon` and whose recomputed bundle `content_hash`es match, `verify_submission` returns `verified=true`, and `benchmark.verify_submission` sets `verified=true`/`verified_at`/`verification`.
9. **Verification rejects a tampered score.** A submission whose `claimed.composite` differs from the reproduced composite by more than `epsilon` → `verified=false`, `status=rejected`, with a reason citing the delta.
10. **Verification rejects a tampered bundle.** A submission whose bundle content no longer matches `replay_content_hashes` → `verified=false`, `bundle_hash_matches=false`, regardless of score proximity.
11. **Deterministic, offline verification.** `verify_submission`’s replay performs **zero** model/network calls (F12 cassette replay) and yields identical results across two runs.
12. **Ranking & tie-break.** `rank_submissions` orders by composite DESC, then verified before unverified on a tie, then earliest `submitted_at`; equal `(composite, verified)` share a competition rank; ranks are 1-based and contiguous per group.
13. **Moderation gate.** A submission is absent from the public leaderboard until an admin `publish`es it (`visibility=public`); `publish` requires `verified=true` unless `force=true` (audited); `flag` sets `status=flagged` and removes it from the board.
14. **Public read works without auth.** With `PUBLIC_LEADERBOARD_ENABLED=true`, `GET /public/leaderboard/{slug}/{version}` returns `200` for an **unauthenticated** request and lists only `visibility=public` submissions, ranked, with `Cache-Control: public`.
15. **Public surface leaks nothing sensitive.** The `PublicLeaderboard`/`PublicSubmissionDetail` responses never contain `submitter_contact`, raw `ArmConfig` secrets (BYOK keys), or raw payloads (prompts/diffs/tool outputs); an integration test injecting a fake key into `config` asserts the substring is absent from every `/public/*` response and from the stored row (redacted on ingest).
16. **Private + disabled isolation.** A `visibility=private` submission never appears on `/public/*`; with `PUBLIC_LEADERBOARD_ENABLED=false` (default) all `/public/*` routes return `404`.
17. **Rate limiting.** Requests to a `/public/*` route beyond `LEADERBOARD_PUBLIC_RATE_LIMIT` from one IP return `429`.
18. **RBAC on management routes.** `POST /runs`, `/verify`, `/publish`, `/flag` require `admin`; `GET` and `POST /submissions` (external submit) require workspace membership; `agent-runner` is read-only on benchmark resources; unauthenticated requests to `/api/v1/benchmarks/*` are rejected `401`.
19. **External submission ingest.** `POST /benchmarks/{slug}/{version}/submissions` with a report + bundle keys creates a `pending` submission bound to the suite’s `content_hash`; a mismatched `suite_content_hash` is rejected `409`; oversize payload (> `BENCHMARK_SUBMISSION_MAX_BYTES`) is rejected `413`.
20. **Reproduce affordance.** `PublicSubmissionDetail.reproduce_command` is the exact `forge bench verify <id>` invocation and `replay_bundle_urls` are short-lived signed URLs; `forge bench verify <id>` against a published verified submission exits `0`.
21. **CLI exit codes.** `forge bench freeze` exits `1` on post-freeze content drift; `forge bench verify` exits `0` (verified) / `1` (rejected); `forge bench run --publish` only publishes after a successful verify.
22. **Schema registration (metadata-driven).** Registering `BenchmarkSuite` + `BenchmarkSubmission` on `Base.metadata` makes `Base.metadata.create_all` (the `0001_baseline.py` upgrade path + the SQLite unit suite) produce `benchmark_suite` and `benchmark_submission` with the documented `UNIQUE (slug, version)` and the leaderboard covering index `(benchmark_suite_id, visibility, verified, composite_score DESC)` (asserted via inspector); `alembic upgrade head` then `downgrade base` round-trips with **no double-create**; and `packages/db/tests/test_models.py::EXPECTED_MODELS` + `test_migration.py::EXPECTED_TABLES` (which assert the model/table set is **exactly** the spec set) are extended with the two new models/tables and stay green. No chained `op.create_table` migration is added while `0001_baseline.py` is the sole migration.
23. **Audit on management/moderation.** Each of `POST /runs`, `POST /submissions` (external submit), `/submissions/{id}/verify`, `/publish`, and `/flag` emits exactly one immutable audit event via the F39 `AuditSink` (actor, submission/suite id, action, reason); denied (`403`) attempts are audited as `denied`.
24. **Human-approval-before-merge preserved.** No benchmark run, replay, or verification ever merges a PR: every `agent_task` benchmark case (inheriting F12 AC18) declares a non-merge `expected_terminal_state` (`pr_opened` | `awaiting_review` | `needs_human_input`), the `agent_target` invokes no merge path, and `forge bench freeze` rejects (exit `1`) any suite whose `agent_task` case sets `expected_terminal_state: merged`.

### Traceability — requirement → criteria

| Spec / F12 requirement | Criteria |
|---|---|
| Frozen, versioned, comparable benchmark (Phase-3 roadmap) | 1, 2, 7, 19 |
| Deterministic, reproducible scoring (Observability "replayable runs") | 3, 4, 5, 11 |
| Verification / gaming-resistance (untrusted input, fail-closed) | 8, 9, 10, 20 |
| Public leaderboard ranking (Phase-3 roadmap) | 12, 13, 14 |
| Secret redaction & tenant/visibility isolation (Security) | 15, 16 |
| Public surface hardening (no anonymous app access exception scoped) | 14, 16, 17 |
| RBAC + audit on management/moderation (Security: RBAC, immutable audit log) | 13, 18, 23 |
| Human approval before merge (non-negotiable) | 24 |
| Spec-gated implementation + hybrid retrieval + MCP read-only (inherited from F12/F05/F08) | 6, 24, §8 |
| Self-hosting privacy default | 16 |
| OSS reproducibility/contribution artifact | 19, 20, 21 |
| Additive on F12 substrate (metadata-driven schema) | 6, 22 |

---

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Write tests first (backend-tdd, ≥80% coverage). Tests in `packages/evaluation/tests/benchmark/` and `apps/api/tests/benchmark/`.

**Key fixtures:**
- `fixture_benchmark` — a 3-case frozen suite (one retrieval, one spec, one agent_task) under `packages/evaluation/tests/fixtures/benchmarks/fixture/1.0.0/` with a precomputed `content_hash`, reusing F12’s `seed_corpus` and a shipped `recorded_cassette` for the agent_task case.
- `report_factory` — builds an `EvalReport` with a chosen `aggregate` for exact-math scoring tests (no DB).
- `tampered_bundle` — a copy of the shipped cassette with one event mutated (breaks `content_hash`).
- `pg` + `minio` — service containers (migrations applied, bundle store live).
- `FakeLLMJudge` (reused from F12) — judge metrics reported-not-gated; verification never depends on the judge.

**Unit (pure, no DB/network):**
- `test_content_hash_order_independent` + `test_content_hash_stable_across_process` (AC1).
- `test_freeze_then_mutation_raises_mismatch` (AC2).
- `test_composite_hand_computed` (AC3, the `0.78` case); `test_composite_recompute_identical`.
- `test_direction_lower_is_better_and_missing_metric_zero` (AC4).
- `test_per_category_covers_all_cases_once` (AC5).
- `test_verify_accepts_within_epsilon` (AC8); `test_verify_rejects_score_delta` (AC9); `test_verify_rejects_bundle_hash_mismatch` using `tampered_bundle` (AC10).
- `test_rank_orders_and_tiebreaks` incl. verified-over-unverified + competition ranks (AC12).

**Integration (pg + minio + F12 fakes):**
- `test_run_submission_scores_and_persists` (AC6) — asserts `eval_case_result` rows + scored `benchmark_submission`.
- `test_run_guards_suite_content_hash` — mutate the fixture cases on disk, assert the run fails without scoring (AC7).
- `test_verify_submission_deterministic_offline` — run twice, byte-identical `VerificationResult`, network-blocking fixture asserts zero outbound calls (AC11).
- `test_publish_requires_verified_and_audits` + `test_flag_removes_from_board` (AC13).
- `test_external_ingest_binds_hash_and_rejects_drift_and_oversize` (AC19).
- `test_submission_config_redacted_on_ingest` — inject a fake key, assert it is `«redacted»` in the stored row (AC15).
- `test_management_actions_emit_audit_events` — assert run/submit/verify/publish/flag each emit exactly one `AuditEvent` (actor, submission/suite id, action, reason) and a `403` is audited as `denied` (AC23).
- `test_agent_target_never_merges` — assert the benchmark `agent_target` exposes/invokes no merge path and a benchmark `agent_task` run terminates in a non-merge state (AC24).

**API tests (`apps/api/tests/benchmark/`, httpx AsyncClient, Celery eager):**
- `test_public_leaderboard_unauthenticated_ok_and_cache_header` (AC14).
- `test_public_excludes_private_and_contact_and_secrets` (AC15) — assert `submitter_contact`/key substring absent in `/public/*`.
- `test_public_routes_404_when_disabled` (AC16).
- `test_public_rate_limited_429` (AC17).
- `test_management_rbac_matrix` (AC18) — admin vs member vs agent-runner vs anon across run/verify/publish/flag/submit.
- `test_public_submission_detail_reproduce_and_signed_urls` (AC20).

**CLI tests:** `test_bench_freeze_exit_on_drift`, `test_bench_verify_exit_codes`, `test_bench_run_publish_only_after_verify` (AC21), and `test_bench_freeze_rejects_merged_terminal_state` (AC24) via `CliRunner`/subprocess.

**Schema tests (metadata-driven, mirrors F12, in `packages/db/tests/`):** `test_benchmark_models_create_all` asserts `Base.metadata.create_all` produces `benchmark_suite`/`benchmark_submission` with the `UNIQUE (slug, version)` and the leaderboard covering index (via SQLAlchemy inspector); `test_benchmark_alembic_roundtrip` runs `alembic upgrade head` → `downgrade base` with no double-create; and `EXPECTED_MODELS`/`EXPECTED_TABLES` are extended with the two new entries and stay green (AC22).

---

## 8. Security & policy considerations

- **Scoped exception to "no anonymous API access" (spec Security → Auth).** The spec mandates authenticated access for the *application*. The public leaderboard is a deliberate, narrowly-scoped exception serving **only moderated, published, payload-free, secret-free** data. It is enforced structurally: a **separate router with no auth dependency and no mutation**, mounted only when `PUBLIC_LEADERBOARD_ENABLED=true` (default `false`), serializing exclusively through the `Public*` models that cannot carry `submitter_contact`, raw config, or raw payloads. This keeps the rest of the API’s no-anonymous-access guarantee intact.
- **Untrusted submitters → fail-closed verification (anti-gaming).** A submitted score is never trusted: `verify_submission` re-derives it from the submitter’s own cassettes via deterministic F12 replay and rejects on any score-delta-beyond-`epsilon` **or** bundle-hash mismatch. Unverified submissions are badged as such; **publishing requires an explicit admin moderation action** (and `verified=true` unless force-overridden and audited).
- **Secret redaction (spec Security → "Secret redaction").** On ingest, `config` and `model_label` pass through F05/F12 `redact_secrets`; BYOK provider keys are **never** persisted in a submission or returned anywhere. Replay bundles were already redacted at record time by F12 (AC14 there). AC15 asserts no injected key survives to storage or the public surface.
- **Frozen-suite integrity = comparability.** A leaderboard is only meaningful if everyone ran the same tasks. `content_hash` binds every submission to an exact frozen task set; mutating a frozen suite is rejected (must bump `version`), preventing silent benchmark drift that would invalidate historical scores.
- **Tenant & visibility isolation.** Internal submissions are workspace-scoped; the public router filters strictly to `visibility='public'`; private submissions and disabled-feature instances are invisible (AC16). System/official submissions use `workspace_id=NULL` and are admin-managed.
- **RBAC & immutable audit (spec Security → RBAC, Audit log).** Run/verify/publish/flag are `admin`-only and emit audit events (actor, submission id, action, reason) per the spec’s immutable-audit requirement; moderation is attributable via `moderated_by`.
- **DoS / abuse bounds.** The public router is per-IP rate-limited (`LEADERBOARD_PUBLIC_RATE_LIMIT`) and cache-fronted (`Cache-Control` + TTL). External ingest enforces `BENCHMARK_SUBMISSION_MAX_BYTES`. Heavy benchmark runs are manual/scheduled (not per-PR) with `EVAL_CONCURRENCY` bounding case parallelism; bundle downloads use short-lived signed URLs, not public object exposure.
- **Self-host privacy default.** `PUBLIC_LEADERBOARD_ENABLED=false` ensures a fresh self-hosted instance never accidentally publishes internal eval data; opting in is an explicit operator decision documented in `docs/self-hosting/security.md`.
- **No policy-engine bypass.** Benchmark runs execute via the same F08/F06 agent flow under the same repo policy + tool gate as any task; F35 adds measurement/publication, never new agent capabilities or scope.
- **Inherited non-negotiables (the benchmark cannot relax them).** Because every benchmark run is an F12 eval run over the F05/F02/F08 substrate, the spec's non-negotiables hold transitively and are not re-implemented or bypassed: (a) **hybrid retrieval** — retrieval benchmark cases score the full F05 pipeline (semantic + BM25 + RRF + Jina rerank, the `retrieval.ndcg_at_k` arm), never a degraded retriever, so the comparison is honest; (b) **spec-gated implementation** — each `agent_task` benchmark case carries an approved `spec_ref` and is graded against that spec's acceptance criteria (F12 AC19), so the benchmark contains no un-spec'd implementation work; (c) **human approval before merge** — the harness performs no merge and every `agent_task` case terminates before `merged` (AC24); (d) **MCP read-only default** — any retrieval that touches MCP runs through F09 query-through, which is read-only by default, so a benchmark run never enables an MCP write; (e) **BYOK** — model/judge credentials are resolved from the F37 per-workspace vault at call time and are never persisted in a submission, config, or replay bundle.

---

## 9. Effort estimate & risk (S/M/L + key risks)

**Overall: M–L.** Smaller than F12 because the entire eval substrate (runner, metrics, replay, store, eval tables, targets) is reused unchanged. The net-new work is the frozen-suite/content-hash + composite scoring (S–M, the deterministic core), the replay-based verification pipeline (M, security-critical), the public unauthenticated + moderated surface (M, the hardening-sensitive path), the internal moderation UI + public leaderboard UI (M), and authoring a real frozen benchmark task set (M). Rough split: models+manifest+scoring S, verification M, submission service+moderation M, public router+rate-limit+cache S, UI (public + internal) M, benchmark authoring M, tests M.

| Risk | Severity | Mitigation |
|---|---|---|
| **Leaderboard gaming** (fabricated/inflated scores) — the headline risk | High | Deterministic replay-based `verify_submission` (score-within-`epsilon` **and** bundle-hash match), unverified badging, **admin moderation required to publish**, audited; AC8/9/10/13 lock it |
| **Secret/payload leakage on the public surface** | High | Dedicated payload-free `Public*` models, redaction on ingest, no `submitter_contact` field in any response, signed short-lived bundle URLs; AC15 asserts no key/contact leaks |
| **Benchmark drift breaks comparability** | High | `content_hash` freeze + version-bump-or-reject; submissions bind `suite_content_hash`; AC1/2/7/19 |
| **Public endpoint as an attack/DoS surface** | Medium | No-auth router is read-only + rate-limited + cached + disabled by default; separate from the authenticated app; AC14/16/17 |
| **Scoring non-determinism / disagreement with F12 metrics** | Medium | Composite is a pure function over F12 `EvalReport.aggregate`; LLM-judged RAGAS metrics are reported-not-scored in the default rubric (gaming-resistant + reproducible); AC3/4/11 |
| **Verification cost / cassette size** | Medium | Replay is offline + bounded concurrency; ingest size cap; verification is async (Celery), not inline on submit |
| **Self-hoster accidentally exposes data** | Low | `PUBLIC_LEADERBOARD_ENABLED=false` default; routes 404 when off; documented in self-hosting security docs |

---

## 10. Key files / paths (exact)

**Core package (extends F12 `forge-eval`):**
- `packages/evaluation/forge_eval/benchmark/__init__.py`
- `packages/evaluation/forge_eval/benchmark/models.py` (NEW — `BenchmarkScoring`/`BenchmarkManifest`/`BenchmarkScore`/`CategoryScore`/`VerificationResult`/`LeaderboardRow`/`SubmissionStatus`/`Visibility`)
- `packages/evaluation/forge_eval/benchmark/manifest.py` (NEW — `load_manifest`/`compute_content_hash`/`freeze`)
- `packages/evaluation/forge_eval/benchmark/scoring.py` (NEW — `compute_benchmark_score`)
- `packages/evaluation/forge_eval/benchmark/verify.py` (NEW — `verify_submission`)
- `packages/evaluation/forge_eval/benchmark/leaderboard.py` (NEW — `rank_submissions`)
- `packages/evaluation/forge_eval/benchmark/errors.py` (NEW)
- `packages/evaluation/forge_eval/benchmarks/forge-swe/1.0.0/{manifest.yaml,cases/**}` (NEW — the first frozen public benchmark)
- `packages/evaluation/tests/benchmark/` + `packages/evaluation/tests/fixtures/benchmarks/fixture/1.0.0/**`

**Data model (metadata-driven — no chained migration while `0001_baseline.py` is sole):**
- `packages/db/forge_db/models/benchmark.py` (NEW — `BenchmarkSuite`, `BenchmarkSubmission` ORM; indexes in `__table_args__`)
- `packages/db/forge_db/models/__init__.py` (EXTEND — register the two models on `Base.metadata` + `__all__`)
- `packages/db/tests/test_models.py` (EXTEND — `EXPECTED_MODELS`), `packages/db/tests/test_migration.py` (EXTEND — `EXPECTED_TABLES`)

**API:**
- `apps/api/forge_api/routers/benchmarks.py` (NEW — authenticated management router; added to `routers/__init__.py::FEATURE_ROUTERS`)
- `apps/api/forge_api/routers/public_leaderboard.py` (NEW — unauthenticated read-only router; conditionally included in `forge_api/main.py` when `PUBLIC_LEADERBOARD_ENABLED=true`)
- `apps/api/forge_api/services/benchmark_service.py` (NEW)
- `apps/api/forge_api/schemas/benchmark.py` (NEW — request/response incl. `Public*` models)
- `apps/api/forge_api/cli/bench.py` (NEW — `forge bench list|freeze|run|submit|verify|leaderboard`)
- `apps/api/tests/benchmark/` (NEW)

**Worker:**
- `apps/worker/forge_worker/tasks/benchmark.py` (NEW — `run_submission`, `verify_submission`; queue `benchmark`)

**Frontend:**
- `apps/web/app/(public)/leaderboard/page.tsx` (NEW)
- `apps/web/app/(public)/leaderboard/[slug]/[version]/page.tsx` (NEW)
- `apps/web/app/(public)/leaderboard/[slug]/[version]/submissions/[id]/page.tsx` (NEW)
- `apps/web/app/(app)/benchmarks/page.tsx` (NEW — run + moderation queue)
- `apps/web/app/(app)/benchmarks/submissions/[id]/page.tsx` (NEW — reuses F10/F12 trace components)
- `apps/web/components/benchmark/{LeaderboardTable,ScoreBreakdown,SubmissionProvenance,ReproducePanel,ModerationQueue}.tsx` (NEW)
- `apps/web/lib/api/benchmark.ts`, `apps/web/lib/api/public-leaderboard.ts` (NEW)

**Infra / CI:**
- `deploy/caddy/Caddyfile`, `deploy/nginx/forge.conf` (extend — `/public/*` route + rate limit/cache)
- `deploy/.env.example`, `deploy/.env.production.example` (extend — `PUBLIC_LEADERBOARD_ENABLED`, `BENCHMARK_*`, `LEADERBOARD_*`)
- `.github/workflows/benchmark-smoke.yml` (NEW — freeze/score/verify smoke, non-gating)

---

## 11. Research references (relevant links from the spec/research report)

- FORGE_SPEC.md §"Phased Roadmap → Phase 3 (V3)" — *"Benchmark suite and public evaluation leaderboard"* (the roadmap line this slice implements).
- FORGE_SPEC.md §"Observability and Evaluation" — "Evaluation Harness" (golden set, *every release runs against the golden test set; regressions block merge*, *replayable workflow runs with step-level inspection*, *A/B evaluation for retrieval strategies and model providers*) and "Key Metrics" (the metric families the composite weights) — the substrate F35 publishes.
- FORGE_SPEC.md §"OSS Strategy" — "Community Artifacts" and "Extension Points" (the public leaderboard + submittable benchmark results are an OSS-adoption artifact and a community extension surface).
- FORGE_SPEC.md §"Security" — Auth (*all routes authenticated; no anonymous API access* — the constraint the public router scopes a deliberate exception to), Secret redaction, RBAC, immutable Audit log, Rate limiting — the invariants the public surface must preserve.
- FORGE_SPEC.md §"Self-Hosting and Deployment" — Caddy/Nginx reverse proxy + required `docs/self-hosting/security.md` (where `PUBLIC_LEADERBOARD_ENABLED` privacy guidance lands).
- `docs/forge-research-report.md` §"What Makes Forge Buildable" #3 *"Eval-first development"* (golden set of 30–100 reproducible pairs before complexity) and §"Hybrid Retrieval" (reranking lifts RAGAS metrics 15–30% — a key cross-config comparison the benchmark surfaces).
- `docs/implementation-slices/v1/F12-eval-harness.md` — the eval substrate (runner, metrics, replay `content_hash`, `eval_run`/`eval_case_result`/`replay_bundle`, A/B) F35 reuses; its §12 explicitly defers this benchmark+leaderboard to Phase 3.
- LangGraph 2026 production guide (ship the smallest agent loop, build a golden eval set first): https://www.reactify-solutions.com/articles/langgraph-production-agents-2026
- Production RAG / RAGAS metrics (the per-metric scores the composite weights): https://lushbinary.com/blog/rag-retrieval-augmented-generation-production-guide/
- LangSmith (optional agent-trace sink reused by the per-task drill-down): https://smith.langchain.com/

---

## 12. Out of scope / future

- **Materialized leaderboard snapshot table / historical rank timelines.** V1 of F35 computes ranking on read (cache-fronted). Persisted point-in-time snapshots and "rank over time" charts are future Observability work.
- **Continuous/scheduled re-benchmarking & canary leaderboards.** Cron-driven nightly benchmark runs and auto-published canary boards ride on the v3 scheduling/automation work; F35 ships manual + Celery-triggered runs.
- **Multi-agent–specific benchmark categories** (coordinator overhead, subagent routing quality). F35 supports `agent_mode=supervised_multi_agent` submissions as a dimension; dedicated multi-agent task categories arrive with the v3 supervised multi-agent slice.
- **Cryptographic submission attestation / signed provenance** (e.g. signing replay bundles with a contributor key). V1 verification is replay+hash-based; signature-based provenance is a future hardening.
- **Cross-instance federated leaderboard** (aggregating submissions across many self-hosted Forges). F35 is per-instance; the canonical public board is one hosted deployment with `PUBLIC_LEADERBOARD_ENABLED=true`.
- **Cost/latency leaderboards as gated dimensions.** Token-cost and latency are captured (F12 `cost`/metrics) and can be displayed, but the default scoring rubric weights quality metrics; cost-normalized leaderboards are a future rubric.
- **In-UI benchmark authoring/editing.** Benchmarks are authored as versioned files in git and frozen via CLI (consistent with F12’s file-based golden suite); a web authoring tool is out of scope.
- **Embedding-based semantic answer correctness in scoring.** Inherits F12’s scope: the benchmark scores the four core deterministic/cassette-derived metric families; deeper RAGAS is future tuning.
